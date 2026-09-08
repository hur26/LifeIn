"""每日摘要 agent —— P0 的唯一目标就是验证它。

03 说 P0 的验收标准是主观的:连续 14 天,你觉得"这条有用"的比例 > 70%。
所以这个模块的设计目标不是"输出好看",而是**让不好的输出能被看出来**:
每一条摘要都要能指回它来自哪封邮件、哪个日程,指不回去的要么标记要么丢掉。

三条规则:

1. **引用了不存在事件的条目直接丢。** 那是确定的幻觉 —— 模型编了一封邮件。
2. **没有引用的条目保留但标记 `unverified`。** 它可能是几封邮件的综合判断,
   丢掉太可惜;但要能数出来,数量一多说明 prompt 得改。
3. **整体解析失败就抛异常,不发半截摘要。** 每天一条,发一条错的比不发更伤
   信任(R4)—— 而调度层会因此告警,不是静默。

`on_uncertain=DO_NOTHING` 指的是第 1 条那种单条不确定,不是"出错就不推送"。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field, ValidationError

from lifein.agents.contract import OnUncertain, agent
from lifein.channels.base import Card, CardSection
from lifein.llm.client import LLMBadResponse, LLMClient
from lifein.llm.prompt import ExternalBlock, build_messages, fields_sent, normalize_ref
from lifein.models.normalized import NormalizedEvent

log = logging.getLogger(__name__)

MAX_EVENTS = 60
"""一次摘要最多送多少条事件。

超出的按时间倒序留新的。不是省钱 —— 是超过这个量之后模型开始平均用力,
反而抓不住重点,而"抓住重点"正是这一期要验证的东西。
"""

MAX_CHARS_PER_EVENT = 1200
"""单条正文送多少字。正文越长越容易把要点稀释掉。"""

CATEGORY_LABELS = {
    "todo": "要做的事",
    "schedule": "日程",
    "fyi": "值得一看",
}

_TASK = (
    "你是一个个人生活助手,负责把用户昨天到今天的邮件与日程压成一条每日摘要。"
    "只挑对用户真正有影响的事:要他做什么、要他在什么时候到场、有什么变化他该知道。"
    "广告、推广、系统通知一律略过。\n\n"
    "输出严格的 JSON,不要加任何解释文字:\n"
    '{"summary": "一句话说清今天最要紧的是什么",\n'
    ' "items": [{"category": "todo|schedule|fyi", "text": "不超过 40 字",\n'
    '            "refs": ["引用的 external_content 的 id"]}]}\n\n'
    "每一条都尽量写上 refs,指明它来自哪条素材。宁可少写几条,也不要写你不确定的事。"
)


class DigestCategory(StrEnum):
    TODO = "todo"
    SCHEDULE = "schedule"
    FYI = "fyi"


class DigestItem(BaseModel):
    category: DigestCategory
    text: str = Field(min_length=1, max_length=120)
    refs: list[str] = Field(default_factory=list)
    unverified: bool = False
    """没有引用任何素材。保留但可数 —— 数量一多说明 prompt 得改。"""


class DigestInput(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    day: date
    events: list[NormalizedEvent]


class DigestOutput(BaseModel):
    summary: str
    items: list[DigestItem] = Field(default_factory=list)
    dropped_hallucinated: int = 0
    """引用了不存在事件、因而被丢掉的条目数。这是最直接的质量指标。"""

    considered_events: int = 0


class DigestFailed(RuntimeError):
    """摘要没生成出来。调度层要告警,不要静默跳过这一天。"""


@dataclass(frozen=True)
class DigestResult:
    output: DigestOutput
    llm_fields_sent: list[str]
    prompt_tokens: int | None
    completion_tokens: int | None


def build_blocks(events: Sequence[NormalizedEvent]) -> list[ExternalBlock]:
    """把事件转成隔离块。结构化字段与正文分开 —— 铁律 9。"""
    blocks: list[ExternalBlock] = []
    for event in sorted(events, key=lambda e: e.occurred_at, reverse=True)[:MAX_EVENTS]:
        fields = {
            "标题": event.title,
            "时间": event.occurred_at.isoformat(),
            "类型": event.kind.value,
        }
        senders = [p.display_name for p in event.parties if p.role.value == "from"]
        if senders:
            fields["来自"] = "、".join(senders)
        blocks.append(
            ExternalBlock(
                source=event.external_ref.source,
                external_id=event.external_ref.external_id,
                text=event.body or "",
                fields=fields,
            )
        )
    return blocks


def _parse(payload: object, known_ids: set[str]) -> DigestOutput:
    # 两边都归一化再比:模型回引时几乎总会把 Message-ID 的尖括号去掉
    known = {normalize_ref(i): i for i in known_ids}
    if not isinstance(payload, dict):
        raise DigestFailed(f"模型返回的顶层不是对象:{type(payload).__name__}")

    summary = str(payload.get("summary", "")).strip()
    if not summary:
        raise DigestFailed("模型没给出 summary")

    items: list[DigestItem] = []
    dropped = 0
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        refs = [str(r) for r in (raw.get("refs") or [])]
        valid = [known[key] for r in refs if (key := normalize_ref(r)) in known]
        if refs and not valid:
            # 引用了一个不存在的事件 = 模型编了一封邮件。确定的幻觉,丢掉
            log.warning("丢弃引用了未知事件的摘要条目:%s", refs)
            dropped += 1
            continue
        try:
            items.append(
                DigestItem(
                    category=raw.get("category", "fyi"),
                    text=str(raw.get("text", "")).strip(),
                    refs=valid,
                    unverified=not valid,
                )
            )
        except ValidationError:
            # 单条不合格不该毁掉整份摘要
            dropped += 1

    return DigestOutput(summary=summary, items=items, dropped_hallucinated=dropped)


@agent(
    name="daily_digest",
    inputs=DigestInput,
    tools=[],  # 事件由调度层取好传进来,这个 agent 本身不碰数据源
    output_schema=DigestOutput,
    on_uncertain=OnUncertain.DO_NOTHING,
    evalset="evals/daily_digest.jsonl",
)
def run_digest(payload: DigestInput, *, llm: LLMClient) -> DigestResult:
    blocks = build_blocks(payload.events)
    if not blocks:
        raise DigestFailed("这一天没有任何事件,不生成摘要")

    messages = build_messages(task=_TASK, blocks=blocks, max_chars_per_block=MAX_CHARS_PER_EVENT)
    response = llm.chat(messages)

    try:
        parsed = response.as_json()
    except LLMBadResponse as exc:
        # 发一条错的比不发更伤信任(R4)。抛出去,让调度层告警
        raise DigestFailed(f"摘要解析失败:{exc}") from exc

    output = _parse(parsed, {b.external_id for b in blocks})
    output.considered_events = len(blocks)
    return DigestResult(
        output=output,
        llm_fields_sent=fields_sent(blocks),
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
    )


def to_card(output: DigestOutput, day: date) -> Card:
    """把摘要渲染成通道中立的卡片。"""
    sections: list[CardSection] = []
    for category in DigestCategory:
        lines = [item.text for item in output.items if item.category is category]
        if lines:
            sections.append(CardSection(heading=CATEGORY_LABELS[category.value], lines=lines))

    footer = f"来自 {output.considered_events} 条事件"
    if output.dropped_hallucinated:
        # 让质量问题出现在你眼前,而不是藏在日志里
        footer += f",丢弃 {output.dropped_hallucinated} 条无法溯源的内容"

    return Card(
        title=f"{day.month} 月 {day.day} 日摘要",
        summary=output.summary,
        sections=sections,
        footer=footer,
    )
