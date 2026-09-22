"""副驾分析 —— `POST /app/copilot/analyze`。契约在 06 §6.16。

单独一个模块而不是塞进 `query.py`,因为它和那一组的性质不一样:
那边全是"读库、回给 App 看",这里是**一次请求打三次外部模型** ——
全系统单次成本最高的入口,也是唯一一个素材由第三方现场构造的入口。

四件事在这个模块里是刻意写死的:

**默认关。** `COPILOT_ENABLED` 不为真时这个路由直接 404,而不是返回空结果 ——
后者会让手机端以为服务端支持、只是这次没读到,然后一直重试。

**必须过额度。** 三次调用,而且是用户想点几次就点几次
(`repos/quota` 那张表里"问答"那一行是同一个道理)。超了回
`degraded: "quota"` 而不是 500:用户要看到"这个月额度用完了",不是一个红叉。

**这个接口自己不是工具,但查记忆要过网关。** 和 §6.6 那条"App 的写操作不过
网关"不同 —— 那一条是因为写的是自己的地盘,这一条是因为它压根没写任何东西。
查记忆调的三个 `memory.*` 都是 L1,照常走 `Gateway.call()`、照常进 `tool_calls`。

**请求体一行不落库。** 唯一落库的是那条审计,而它只有字段名和长度,
没有内容(ADR-038:副驾读记忆,不写记忆)。
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from lifein.agents.copilot import (
    ChatMsg,
    CopilotFailed,
    CopilotInput,
    CopilotOutput,
    Judgement,
    analyze,
)
from lifein.agents.qa import RecalledFact
from lifein.api.deps import AppCaller, NowDep, SessionDep, SettingsDep
from lifein.config import Settings
from lifein.governance.audit import ToolCallRecord
from lifein.governance.gateway import CallContext, Denied, Gateway
from lifein.governance.registry import ToolLevel
from lifein.llm.client import LLMClient, LLMError
from lifein.models.normalized import Trust
from lifein.repos import quota
from lifein.repos.tool_calls import PostgresAuditSink, record_tool_call

log = logging.getLogger(__name__)

router = APIRouter(prefix="/app", tags=["copilot"])

ALLOWED_APPS = frozenset({"wechat", "qq"})
"""认得的聊天 App。

**不认的直接 422,不是"当成 wechat 处理"。** [03 的 P5](../../docs/03-roadmap.md#p5--副驾)
首期只做这两个,而一个未知的 `app` 值意味着手机端装了个比服务端新的版本 ——
那时糊弄过去会让用户拿到一份按错误假设算出来的建议。
"""

MAX_INBOUND = 200
"""单次请求最多接受多少条消息。

比 `COPILOT_MAX_MESSAGES` 宽:那个是进 prompt 的条数,这个是**别让 body 无限大**。
两个数不是一回事,合成一个的话手机端多传几条就会被 422,
而它完全不知道服务端的窗口是多少。
"""


class MsgIn(BaseModel):
    """一条消息。

    **字段是宽松的**,和 §6.4 采集上报那条同一条规矩:一行坏的不该让整批 422。
    `side` 写错、`text` 是空的,都在下面被丢掉并计数。
    """

    model_config = ConfigDict(extra="ignore")

    side: str = ""
    text: str = ""


class AnalyzeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    device_id: str = ""
    app: str = ""
    title: str = ""
    messages: list[MsgIn] = Field(default_factory=list, max_length=MAX_INBOUND)
    history: list[MsgIn] = Field(default_factory=list, max_length=MAX_INBOUND)
    capture_note: str = ""


class ContextOut(BaseModel):
    facts_used: int = 0
    history_used: int = 0


class AnalyzeOut(BaseModel):
    """固定形状:每个键都在(06 §6.16)。

    "少一个键"和"这个值是 0"在客户端看起来一样,而前者是服务端出了问题、
    后者是这次真的没有 —— 客户端要能区分。
    """

    judgement: Judgement
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    context: ContextOut = Field(default_factory=ContextOut)
    capture_note: str = ""
    degraded: str | None = None
    dropped: int = 0
    """被丢掉的坏消息条数。**给出来而不是静默丢** —— 手机端适配器写错了
    (比如 `side` 判反)的表现会是这个数一直不为零,而那是唯一能被发现的地方。"""


def get_llm(request: Request) -> LLMClient:
    """从装配好的服务里取模型客户端。

    走 `app.state.services` 而不是每次新建:客户端里有连接池、重试策略和单价,
    每个请求造一个等于把那些配置复制一遍,而复制出来的那份迟早和主份不一样。
    """
    services = getattr(request.app.state, "services", None)
    if services is None:  # pragma: no cover - 装配漏了才会走到
        raise HTTPException(status_code=503, detail="服务未装配")
    return services.llm


LLMDep = Annotated[LLMClient, Depends(get_llm)]


@router.post("/copilot/analyze", response_model=AnalyzeOut)
def analyze_chat(
    body: AnalyzeIn,
    caller: AppCaller,
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
    llm: LLMDep,
) -> AnalyzeOut:
    _require_enabled(settings)
    _require_same_device(body, caller)

    if body.app.strip() not in ALLOWED_APPS:
        raise HTTPException(status_code=422, detail="不认识这个聊天 App")

    messages, dropped_msgs = _clean(body.messages)
    history, dropped_hist = _clean(body.history)
    if not messages:
        # 一条都没读到就不该发这个请求。回 422 而不是回一个空判断 ——
        # 后者会让手机端以为"服务端觉得这段对话没什么可说的"
        raise HTTPException(status_code=422, detail="messages 里没有可用的消息")

    try:
        quota.guard(
            caller.user_id,
            session,
            now=now,
            cap=_cap(settings),
        )
    except quota.QuotaExceeded as exc:
        # 不是 500。用户刚点了一下,要看到原因
        log.info("副驾额度用完:user=%s %s", caller.user_id, exc)
        return AnalyzeOut(
            judgement=Judgement(),
            capture_note=body.capture_note,
            degraded="quota",
            dropped=dropped_msgs + dropped_hist,
        )

    relationship, facts = _recall(
        caller.user_id,
        session,
        title=body.title,
        settings=settings,
    )

    payload = CopilotInput(
        app=body.app.strip(),
        title=body.title.strip(),
        messages=messages[-settings.copilot_max_messages :],
        history=history[-settings.copilot_max_history :],
        facts=facts,
        relationship=relationship,
    )

    try:
        result = analyze(payload, llm=llm)
    except (CopilotFailed, LLMError) as exc:
        # 悬浮窗要当场说出来 —— 用户正看着聊天窗等结果(架构 §8.7)
        log.warning("副驾分析失败:%s", exc)
        raise HTTPException(status_code=503, detail="这次没读懂,再试一下") from exc

    _audit(
        caller.user_id,
        session,
        payload=payload,
        result=result,
    )

    return _to_response(
        result.output,
        capture_note=body.capture_note,
        facts_used=len(facts),
        history_used=len(payload.history),
        dropped=dropped_msgs + dropped_hist,
    )


# ---------- 内部 ----------


def _require_enabled(settings: Settings) -> None:
    if not settings.copilot_enabled:
        # 404 而不是 403:关掉的时候这个接口就是不存在
        raise HTTPException(status_code=404, detail="Not Found")


def _require_same_device(body: AnalyzeIn, caller: AppCaller) -> None:
    """body 里的 device_id 要和 token 里那个一致。

    和 §6.4 那条同源:签名认出来的设备是权威,body 里那个只是副本。
    对不上说明有人把别的设备的 body 发过来了。
    """
    if body.device_id.strip() and body.device_id.strip() != caller.device_id:
        raise HTTPException(status_code=422, detail="device_id 与凭据不符")


def _cap(settings: Settings) -> Decimal | None:
    """0 表示不限(config 里那条注释)。**不要把 0 当成上限传下去** ——
    那会让每个人的副驾在第一次调用就被拦死。"""
    return Decimal(str(settings.monthly_cost_cap_cny)) if settings.monthly_cost_cap_cny else None


def _clean(rows: list[MsgIn]) -> tuple[list[ChatMsg], int]:
    """坏行丢掉并计数,不让一行坏的把整批 422 掉(§6.4 那条规矩)。"""
    kept: list[ChatMsg] = []
    dropped = 0
    for row in rows:
        text = row.text.strip()
        if not text or row.side not in ("me", "other"):
            dropped += 1
            continue
        kept.append(ChatMsg(side=row.side, text=text))
    return kept, dropped


def _recall(
    user_id: str,
    session: Session,
    *,
    title: str,
    settings: Settings,
) -> tuple[str, list[RecalledFact]]:
    """查这个人是谁、有哪些相关事实。**两次调用都过网关。**

    查不到就是查不到 —— 陌生人也能用副驾,只是候选里不会带上"他不吃香菜"。
    所以这里任何一步失败都只是少一点背景,不让整次分析挂掉。
    """
    gateway = Gateway(PostgresAuditSink(user_id, session))
    ctx = CallContext(
        user_id=user_id,
        agent="copilot",
        # 触发这次调用的是用户点的那一下,不是对方发的消息。
        # 三个工具都是 L1、不看 trust,但如实记下来才对得上"谁让查的"
        trust=Trust.USER_INPUT,
        session=session,
    )

    relationship = ""
    name = title.strip()
    if name:
        try:
            found = gateway.call(ctx, "memory.search_entities", {"name": name})
        except (Denied, RuntimeError) as exc:
            log.info("副驾查实体失败,当陌生人处理:%s", exc)
            found = []
        if found:
            top = found[0]
            relationship = f"{top.get('name', name)}({top.get('kind', '')})"

    facts: list[RecalledFact] = []
    try:
        rows = gateway.call(
            ctx,
            "memory.recall_facts",
            {"query": name, "limit": settings.copilot_max_facts},
        )
    except (Denied, RuntimeError) as exc:
        log.info("副驾查事实失败,这次不带记忆:%s", exc)
        rows = []

    for row in rows or []:
        statement = str(row.get("statement", "")).strip()
        if not statement:
            continue
        facts.append(
            RecalledFact(
                fact_id=str(row.get("fact_id") or row.get("id") or ""),
                statement=statement,
                confidence=float(row.get("confidence") or 0.0),
                confirmed_by_user=bool(row.get("confirmed_by_user")),
            )
        )
    return relationship, facts


def _audit(user_id: str, session: Session, *, payload: CopilotInput, result) -> None:
    """记这次模型调用。**只有字段名和长度,没有内容。**

    `args_digest` 里刻意不放任何一句原话:这张表是"我到底把什么发给了外部
    供应商"的唯一答案(09 §5),而它自己不该变成第二份聊天记录。
    """
    record_tool_call(
        user_id,
        session,
        ToolCallRecord(
            user_id=user_id,
            agent="copilot",
            tool_name="llm.chat",
            level=ToolLevel.L1,
            args_digest={
                "app": payload.app,
                "messages": {"type": "list", "len": len(payload.messages)},
                "history": {"type": "list", "len": len(payload.history)},
                "facts": {"type": "list", "len": len(payload.facts)},
                "title_len": len(payload.title),
            },
            llm_fields_sent=result.llm_fields_sent,
            result_status="allowed",
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        ),
    )


def _to_response(
    output: CopilotOutput,
    *,
    capture_note: str,
    facts_used: int,
    history_used: int,
    dropped: int,
) -> AnalyzeOut:
    return AnalyzeOut(
        judgement=output.judgement,
        candidates=[c.model_dump() for c in output.candidates],
        context=ContextOut(facts_used=facts_used, history_used=history_used),
        capture_note=capture_note,
        degraded=output.degraded,
        dropped=dropped,
    )
