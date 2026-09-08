"""回答企微里发来的提问。

和每日摘要的区别是**它由用户触发**,这带来两条不同的规矩:

**不写 `push_log`。** 那张表是频率闸门的计数来源,而闸门管的是"非用户主动
触发的推送每天不超过 3 条"(产品定义 §5)。把回复记进去,等于你多问几句
就把自己当天的摘要额度吃光了。

**认不出的人直接丢,连回复都不给。** 企业里任何人都能给自建应用发消息。
回一句"你不是这个系统的用户"等于告诉对方这个地址是活的、后面有东西 ——
不回复才是正确的沉默。

这里没有把用户的提问当成指令去执行任何操作:问答只读、只回话。
提问本身进 prompt 的位置由 `build_messages` 保证(qa.py 里说明了三层结构)。

**P1 起多了一步检索。** 顺序是:先让模型从问句里认出要查谁(`plan_recall`),
再过网关调 L1 记忆工具,最后带着检索结果回答。检索这一步整个是"能失败的" ——
线索解析不出来、工具报错、一条都没查到,都只是让答案退回到"只看最近七天",
不让整次问答失败。用户问一句话,最坏结果应该是答得不够好,不是没有回音。

**工具走网关,不直接 import。** 那三次读取因此留在 `tool_calls` 里,
"它凭什么这么回答"才复盘得了。`CallContext.agent` 填 `qa` —— 网关的双重门
校验的是"这次调用代表哪个 agent",不是谁写的这行代码。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from lifein.agents.qa import (
    QaFailed,
    QaInput,
    RecalledEvent,
    RecalledFact,
    answer,
    plan_recall,
)
from lifein.channels.base import Card, Channel, InboundMessage
from lifein.governance.audit import ToolCallRecord
from lifein.governance.gateway import CallContext, Gateway
from lifein.governance.registry import ToolLevel
from lifein.llm.client import LLMClient, LLMError
from lifein.models.normalized import Trust
from lifein.repos import raw_events, users
from lifein.repos.tool_calls import PostgresAuditSink, record_tool_call

log = logging.getLogger(__name__)

GatewayFactory = Callable[[str, Session], Gateway]


def default_gateway(user_id: str, session: Session) -> Gateway:
    """按次构造网关:审计沿用调用方的事务,业务回滚时审计跟着回滚。"""
    return Gateway(PostgresAuditSink(user_id, session))

LOOKBACK = timedelta(days=7)
"""不做任何检索时,兜底看多久的事件。

七天是个折中:再长就装不下,再短连"上周"都答不了。**它现在是兜底而不是上限**
—— 超出这个窗口的事情靠 `memory.recent_events_with` 按人检索回来。
等第 5 片的向量召回上线,这个常量还能再缩。
"""

MAX_RECALLED_EVENTS = 6
MAX_RECALLED_FACTS = 8
"""检索回来的东西最多带多少进 prompt。

带太多会把最近七天那些真正相关的事件挤出去 —— 检索是补充,不是替代。
"""

UNSUPPORTED_REPLY = "我这边只认文字消息,图片和语音还看不了。"
FAILED_REPLY = "这次没答上来,过会儿再问我一次。"

MAX_QUESTION_CHARS = 500


@dataclass(frozen=True)
class QaDeps:
    llm: LLMClient
    channel: Channel
    resolve_user: Callable[[Session, InboundMessage], users.User | None]
    """把"通道内的发送者"换成本系统的用户。

    企微认 UserID、iLink 认对方的 user id,**这个映射是通道的事,不是问答的事**。
    注入进来,问答就不用为每加一个入站通道改一次。
    """

    gateway_factory: GatewayFactory | None = None
    """记忆检索的入口。**None 就是不检索**,退回"只看最近七天"。

    是工厂而不是现成的网关:审计要和这次问答写在**同一个事务**里
    (`PostgresAuditSink` 持有 session),而 user_id 要等 `resolve_user`
    跑完才知道。两样都是每条消息才有的东西,只能到那时候再拼。

    留成可选不是偷懒:记忆层还没数据的部署照样该能问答,
    而"问一句话得到一句报错"比"答得浅一点"糟得多。
    """


@dataclass
class ReplyResult:
    handled: bool
    user_id: str | None = None
    reason: str | None = None
    """没处理的原因。unknown_user / disabled / unsupported_type / empty。"""


def handle_message(
    session: Session,
    *,
    message: InboundMessage,
    deps: QaDeps,
    now: datetime,
    lookback: timedelta = LOOKBACK,
) -> ReplyResult:
    user = deps.resolve_user(session, message)
    if user is None:
        # 陌生人。不回复 —— 回复等于告诉对方这个地址是活的
        log.warning("收到未知用户的消息:channel=%s sender=%s", message.channel, message.sender)
        return ReplyResult(handled=False, reason="unknown_user")

    if not user.active:
        log.info("用户 %s 已停用,忽略消息", user.id)
        return ReplyResult(handled=False, user_id=user.id, reason="disabled")

    if message.msg_type != "text":
        _reply(deps, user.id, message, UNSUPPORTED_REPLY)
        return ReplyResult(handled=False, user_id=user.id, reason="unsupported_type")

    question = message.content.strip()[:MAX_QUESTION_CHARS]
    if not question:
        return ReplyResult(handled=False, user_id=user.id, reason="empty")

    events = raw_events.fetch_normalized_between(user.id, session, start=now - lookback, end=now)
    recalled, recalled_facts = _recall(session, user_id=user.id, question=question, deps=deps)

    try:
        result = answer(
            QaInput(
                question=question,
                events=events,
                recalled=recalled,
                facts=recalled_facts,
            ),
            llm=deps.llm,
        )
    except (QaFailed, LLMError) as exc:
        # 回一句人话,不回堆栈。用户不需要知道是解析失败还是超时
        log.warning("问答失败:%s", exc)
        _reply(deps, user.id, message, FAILED_REPLY)
        return ReplyResult(handled=False, user_id=user.id, reason="qa_failed")

    record_tool_call(
        user.id,
        session,
        ToolCallRecord(
            user_id=user.id,
            agent="qa",
            tool_name="llm.chat",
            level=ToolLevel.L1,
            args_digest={
                "question_len": len(question),
                "events": {"type": "list", "len": len(events)},
                "recalled": {"type": "list", "len": len(recalled)},
                "facts": {"type": "list", "len": len(recalled_facts)},
            },
            llm_fields_sent=result.llm_fields_sent,
            result_status="allowed",
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        ),
    )

    _reply(deps, user.id, message, result.output.answer, refs=result.output.refs)
    return ReplyResult(handled=True, user_id=user.id)


def _recall(
    session: Session,
    *,
    user_id: str,
    question: str,
    deps: QaDeps,
) -> tuple[list[RecalledEvent], list[RecalledFact]]:
    """按问句去记忆里捞素材。**整个过程允许失败,失败就当没检索。**

    失败的形态有好几种:模型认不出人名、工具报错、库里根本没有这个人。
    它们的共同点是"答案会差一点",而把它们变成异常的共同点是"用户没有回音"
    —— 后者严重得多,所以这里一律吞掉并记日志。
    """
    if deps.gateway_factory is None:
        return [], []

    planned = plan_recall(question, llm=deps.llm)
    plan = planned.plan
    _record_llm_call(
        session,
        user_id=user_id,
        stage="plan",
        question_len=len(question),
        prompt_tokens=planned.prompt_tokens,
        completion_tokens=planned.completion_tokens,
    )
    gateway = deps.gateway_factory(user_id, session)
    ctx = CallContext(
        user_id=user_id,
        agent="qa",  # 代表哪个 agent,不是谁写的这行代码
        trust=Trust.USER_INPUT,  # 问句是用户本人打的
        session=session,
    )

    events: list[RecalledEvent] = []
    if plan.person:
        try:
            found = gateway.call(
                ctx,
                "memory.recent_events_with",
                {"name": plan.person, "limit": MAX_RECALLED_EVENTS},
            )
            events = [RecalledEvent(**item) for item in found.get("events", [])]
        except Exception:  # noqa: BLE001
            log.exception("按人检索事件失败,这次不带历史")

    facts: list[RecalledFact] = []
    try:
        found = gateway.call(
            ctx,
            "memory.recall_facts",
            {"query": plan.keywords[0] if plan.keywords else "", "limit": MAX_RECALLED_FACTS},
        )
        facts = [
            RecalledFact(
                fact_id=item["fact_id"],
                statement=item["statement"],
                confidence=item["confidence"],
                confirmed_by_user=item["confirmed_by_user"],
            )
            for item in found
        ]
    except Exception:  # noqa: BLE001
        log.exception("回忆事实失败,这次不带记忆")

    return events, facts


def _record_llm_call(
    session: Session,
    *,
    user_id: str,
    stage: str,
    question_len: int,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> None:
    """把一次模型调用记进审计。

    检索线索那一次也要记:**它同样把用户的问句发给了外部供应商**(R12),
    也同样花钱。少记一次,成本对不上、"发出去了什么"也答不全。
    """
    record_tool_call(
        user_id,
        session,
        ToolCallRecord(
            user_id=user_id,
            agent="qa",
            tool_name="llm.chat",
            level=ToolLevel.L1,
            args_digest={"stage": stage, "question_len": question_len},
            llm_fields_sent=[],
            result_status="allowed",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


def _reply(
    deps: QaDeps,
    user_id: str,
    message: InboundMessage,
    text: str,
    refs: Sequence[str] = (),
) -> None:
    card = Card(
        # 把问题放标题里:企微的对话流里,回复和提问之间可能隔了别的消息
        title=message.content.strip()[:40] or "回答",
        summary=text,
        footer=f"依据 {len(refs)} 条记录" if refs else None,
    )
    try:
        deps.channel.send(user_id, card)
    except Exception:  # noqa: BLE001
        # 回不出去就算了:用户还在那儿等着,但重试也多半失败,
        # 而这条不像每日摘要那样"错过就没了"—— 他可以再问一次
        log.exception("回复发送失败,user=%s", user_id)
