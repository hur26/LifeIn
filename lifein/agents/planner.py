"""日程与待办提取 agent —— 从邮件和群消息里认出"周三下午三点开会"。

**这个 agent 的产出会变成你日历里的东西**,所以它和摘要、记忆是两种气质:
后两者错了是"看着别扭",这个错了是**日历被污染**。03 的退出条件写得很直白 ——
"日历被污染比没有这个功能糟得多,你会不再信任日历本身"。

于是这里的规矩比别处严:

**日程一律走待确认,不直接写日历。** 03 的验收标准是"误报进日历的条数为 0",
不是"低于多少" —— 一个 0 指标没法靠置信度阈值保证,只能靠人点一下。
待办不同:它只出现在自己的列表里,多一条一眼就划掉了,所以够有把握的直接建。
(**重评触发条件**:待确认队列里日程的拒绝率连续两周低于 5%,
那时可以给高置信度的日程开直通,阈值从 0.9 起步。)

**时间由模型解析,但锚点是规则给的。** "周三下午三点"要变成绝对时间,
必须知道这封邮件是什么时候发的 —— 那个时间戳来自 `occurred_at`,不是让模型
猜"今天几号"。中文相对时间没有可靠的规则实现(分词和词典在"下下周三""月底前"
上都会翻车),所以解析交给模型;**但解析结果要过一遍规则**:
没有时区的、已经过去的,一律不建。铁律 9 管的是"规则拿得到的字段",
锚点是规则拿到的,相对时间不是。

**`on_uncertain=PENDING_CONFIRMATION`** —— 铁律 7 说拿不准时的默认行为是不动作,
而"不动作"在这里的形态就是进队列,不是默默丢掉:丢掉的话你永远不知道
它漏了什么,而漏报率是本期的验收指标之一。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, ValidationError

from lifein.agents.contract import OnUncertain, agent
from lifein.llm.client import LLMBadResponse, LLMClient
from lifein.llm.prompt import ExternalBlock, build_messages, fields_sent, normalize_ref
from lifein.models.normalized import PartyRole, Trust
from lifein.repos.raw_events import StoredEvent

log = logging.getLogger(__name__)

MAX_EVENTS = 60
MAX_CHARS_PER_EVENT = 1200
TITLE_MAX = 120

TODO_DIRECT_MIN = 0.7
"""待办直接建的置信度下限。低于它进待确认。

比日程宽松是因为代价不对称:待办列表里多一条,你划掉它花一秒;
日历里多一条,你会开始怀疑日历本身。
"""

SCHEDULE_ALWAYS_CONFIRM = True
"""日程一律进待确认。理由见模块文档,重评触发条件也在那里。"""


class ItemKind(StrEnum):
    SCHEDULE = "schedule"
    TODO = "todo"


class Route(StrEnum):
    DIRECT = "direct"
    """够有把握,直接过 L2 工具建出来。"""

    PENDING = "pending"
    """进待确认队列。**这是"不动作"的形态,不是失败。**"""


class ExtractedItem(BaseModel):
    kind: ItemKind
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    starts_at: datetime | None = None
    provenance: list[int] = Field(min_length=1)
    """`raw_events.id`。空的进不了这个模型 —— 待办也要说得出出处(铁律 5)。"""

    confidence: float = Field(ge=0.0, le=1.0)
    route: Route
    reason: str = ""
    """走待确认的原因,直接写进 `pending_confirmations.reason`。"""


class PlannerInput(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    events: list[StoredEvent]
    now: datetime
    """判断"这个时间是不是已经过去了"的基准。由调用方给,不在这里取当前时间
    —— 补跑历史窗口时,"现在"不是此刻。"""


class PlannerOutput(BaseModel):
    items: list[ExtractedItem] = Field(default_factory=list)
    dropped_ungrounded: int = 0
    dropped_past: int = 0
    """时间已经过去的条目。"上周三开会"是在描述已发生的事,不是要你去。"""

    considered_events: int = 0


@dataclass(frozen=True)
class PlannerResult:
    output: PlannerOutput
    llm_fields_sent: list[str]
    prompt_tokens: int | None
    completion_tokens: int | None


class PlannerFailed(RuntimeError):
    """整次提取失败。**不是"这次没提到东西"** —— 那是正常的,返回空结果。"""


_TASK = (
    "你是一个个人生活助手,从素材里找出**需要用户去做或去参加的事**。\n\n"
    "两类:\n"
    '1. 日程 —— 有明确时间的,比如"周三下午三点开会"\n'
    '2. 待办 —— 没有明确时间的,比如"帮我带个充电器"\n\n'
    "不要提取:\n"
    "- 已经发生完、不需要用户再做什么的事\n"
    "- 广告、推广、系统通知里的活动\n"
    "- 别人之间的事,与用户无关的\n\n"
    "每条素材都给了它自己的时间。**相对时间要按那条素材的时间换算成绝对时间**,"
    "并带上时区偏移(如 2026-09-09T15:00:00+08:00)。算不出确切时间的,"
    "就当成待办,不要猜。\n\n"
    "输出严格的 JSON,不要加任何解释文字:\n"
    '{"items": [{"kind": "schedule 或 todo",\n'
    '            "title": "不超过 30 字",\n'
    '            "starts_at": "ISO 时间,待办填 null",\n'
    '            "refs": ["这条来自哪些素材的 id"],\n'
    '            "confidence": 0 到 1 之间的小数}]}\n\n'
    "每一条都必须写 refs。**没有 refs 的会被直接丢弃**,不要写。\n"
    "宁可少提一条,也不要提你不确定的。"
)


def build_blocks(
    stored: Sequence[StoredEvent], *, max_events: int = MAX_EVENTS
) -> list[ExternalBlock]:
    """把事件包成隔离块。**每条都带上自己的发生时间** —— 那是相对时间的锚点。"""
    blocks: list[ExternalBlock] = []
    for item in sorted(stored, key=lambda s: s.event.occurred_at, reverse=True)[:max_events]:
        event = item.event
        fields = {"标题": event.title, "时间": event.occurred_at.isoformat()}
        senders = [p.display_name for p in event.parties if p.role is PartyRole.FROM]
        if senders:
            fields["发件人"] = "、".join(senders)
        if event.location:
            fields["地点"] = event.location
        blocks.append(
            ExternalBlock(
                source=event.external_ref.source,
                external_id=event.external_ref.external_id,
                text=event.body or "",
                fields=fields,
            )
        )
    return blocks


def _parse_time(value: object) -> datetime | None:
    """解析模型给的时间。**无时区一律当成解析失败。**

    不拿用户时区去补:一条错了八小时的日程,比一条没有时间的待办糟得多 ——
    后者你看一眼就知道要自己排,前者会让你准时错过。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _route_for(kind: ItemKind, confidence: float) -> tuple[Route, str]:
    if kind is ItemKind.SCHEDULE and SCHEDULE_ALWAYS_CONFIRM:
        # 03 的验收标准是"误报进日历的条数为 0",0 指标只能靠人点一下
        return Route.PENDING, "check_failed"
    if confidence < TODO_DIRECT_MIN:
        return Route.PENDING, "low_confidence"
    return Route.DIRECT, ""


def _parse(payload: object, index: dict[str, StoredEvent], *, now: datetime) -> PlannerOutput:
    if not isinstance(payload, dict):
        raise PlannerFailed(f"模型返回的顶层不是对象:{type(payload).__name__}")

    output = PlannerOutput()
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict):
            output.dropped_ungrounded += 1
            continue

        refs = [str(r) for r in (raw.get("refs") or [])]
        sources = [index[key] for r in refs if (key := normalize_ref(r)) in index]
        if not sources:
            # 引用不存在的素材 = 模型编了一封邮件。和记忆那边同一条规矩
            log.info("丢弃指不回事件的条目:refs=%s", refs)
            output.dropped_ungrounded += 1
            continue

        starts_at = _parse_time(raw.get("starts_at"))
        kind = ItemKind.SCHEDULE if starts_at else ItemKind.TODO

        if starts_at and starts_at <= now:
            # "上周三开会"是在描述已发生的事,建出来只会变成一条过期提醒
            output.dropped_past += 1
            continue

        confidence = _clamp(raw.get("confidence"))
        route, reason = _route_for(kind, confidence)
        try:
            output.items.append(
                ExtractedItem(
                    kind=kind,
                    title=str(raw.get("title", "")).strip(),
                    starts_at=starts_at,
                    provenance=sorted({s.event_id for s in sources}),
                    confidence=confidence,
                    route=route,
                    reason=reason,
                )
            )
        except ValidationError:
            output.dropped_ungrounded += 1

    return output


def _clamp(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5
    return min(1.0, max(0.0, number))


def trust_of(sources: Sequence[StoredEvent]) -> Trust:
    """依据里有一条是外部的,整条就是外部的。

    这个值最终进 `CallContext.trust`。L2 允许由外部内容触发(建的东西都在你
    自己的地盘里且可回滚),但它照样要被如实记进审计 —— 哪天要查"这条日程
    到底是谁让建的",看的就是它。
    """
    return (
        Trust.USER_INPUT
        if sources and all(s.event.trust is Trust.USER_INPUT for s in sources)
        else Trust.EXTERNAL
    )


@agent(
    name="planner",
    inputs=PlannerInput,
    tools=["todo.create"],  # 只能建,不能改不能删 —— 提取器没有理由动已有的东西
    output_schema=PlannerOutput,
    on_uncertain=OnUncertain.PENDING_CONFIRMATION,
    evalset="evals/planner.jsonl",
)
def extract(
    payload: PlannerInput, *, llm: LLMClient, max_events: int = MAX_EVENTS
) -> PlannerResult:
    blocks = build_blocks(payload.events, max_events=max_events)
    if not blocks:
        return PlannerResult(
            output=PlannerOutput(), llm_fields_sent=[], prompt_tokens=None, completion_tokens=None
        )

    messages = build_messages(task=_TASK, blocks=blocks, max_chars_per_block=MAX_CHARS_PER_EVENT)
    response = llm.chat(messages)

    try:
        parsed = response.as_json()
    except LLMBadResponse as exc:
        raise PlannerFailed(f"提取结果解析失败:{exc}") from exc

    sent = {b.external_id for b in blocks}
    index = {
        normalize_ref(item.event.external_ref.external_id): item
        for item in payload.events
        if item.event.external_ref.external_id in sent
    }
    output = _parse(parsed, index, now=payload.now)
    output.considered_events = len(blocks)

    return PlannerResult(
        output=output,
        llm_fields_sent=fields_sent(blocks),
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
    )
