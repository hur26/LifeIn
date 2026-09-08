"""记忆抽取 agent —— 第三个 agent,把事件流里的东西变成实体和事实。

**这个 agent 一半不用模型。** 铁律 9:能用规则拿到的字段不许交给 LLM。
参与方的姓名、邮箱、企微 userid 已经躺在归一化骨架的 `parties` 里了,
它们是解析邮件头得来的,精确、可复现、免费。把它们送去让模型"识别一下人名"
既是花钱买不确定性,更是把通讯录整份交给外部供应商(R12)。

所以分工是:

    实体(谁出现过、他的邮箱是什么)   纯规则,从 parties 里读
    事实(他是谁、他答应了什么)       模型,从正文里读

模型这一半有一条比摘要更严的规矩:**指不回具体事件的事实直接丢。**
摘要允许保留无引用条目(标 `unverified`),因为一条综合判断丢了可惜;
记忆不允许 —— 铁律 5 说没有来源的记忆不许落库,而"来源"就是这里的 refs。
丢掉的条数记在输出里,数量一多说明 prompt 该改了。

`on_uncertain=DO_NOTHING`:**少记一条无所谓,记错一条要命。**
记忆是会被反复引用的东西,一条错的事实会污染此后每一次回答,
而且用户看到的是"它怎么突然这么说",追不到是哪天哪封邮件带进来的。

这个 agent 只产出结构,**不落库**。写库的是调度层,它拿着 `MemoryOutput`
去调仓储 —— 那两层的规则(external 封顶 0.6、被否定的不再写回)在仓储里,
不在这里重复一遍。规则写两遍就一定会有一天只改了一遍。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field, ValidationError

from lifein.agents.contract import OnUncertain, agent
from lifein.llm.client import LLMBadResponse, LLMClient
from lifein.llm.prompt import ExternalBlock, build_messages, fields_sent, normalize_ref
from lifein.models.normalized import NormalizedEvent, Party, PartyRole, Trust
from lifein.repos.entities import AliasType, EntityKind, alias_type_for_identifier, normalize_alias
from lifein.repos.raw_events import StoredEvent

log = logging.getLogger(__name__)

MAX_EVENTS = 60
"""一次抽取最多看多少条事件。超出的按时间倒序留新的。"""

MAX_CHARS_PER_EVENT = 1200
STATEMENT_MAX = 80
"""事实的长度上限。

超过这个长度的多半不是一条事实,是模型把整封邮件复述了一遍 ——
那种"事实"没法被否定、没法被检索,只会在记忆里占位置。
"""

_MERCHANT_ROLES = frozenset({PartyRole.MERCHANT})

_TASK = (
    "你是一个个人生活助手的记忆模块。从素材里抽取**值得长期记住**的事实。\n\n"
    "只抽两类:\n"
    "1. 关系与身份:某人是谁、在哪个部门、和用户是什么关系\n"
    "2. 承诺与约定:用户答应了谁什么事,谁答应了用户什么\n\n"
    "不要抽:\n"
    "- 一次性的事件本身(它已经在事件流里了,不需要在记忆里再存一份)\n"
    "- 广告、推广、系统通知里的内容\n"
    "- 素材里没有明说、要靠推测才能得出的事\n\n"
    "输出严格的 JSON,不要加任何解释文字:\n"
    '{"facts": [{"statement": "一句不超过 40 字的陈述",\n'
    '            "refs": ["这条来自哪些素材的 id"],\n'
    '            "confidence": 0 到 1 之间的小数}]}\n\n'
    "每一条都必须写 refs。**没有 refs 的条目会被直接丢弃**,不要写。\n"
    "宁可一条都不抽,也不要抽你不确定的。"
)


class EntitySighting(BaseModel):
    """一次"某人出现在某条事件里"。**纯规则产物,模型碰不到它。**"""

    kind: EntityKind
    name: str = Field(min_length=1)
    identifier: str | None = None
    identifier_type: AliasType | None = None
    seen_at: datetime
    event_id: int


class ExtractedFact(BaseModel):
    statement: str = Field(min_length=1, max_length=STATEMENT_MAX)
    provenance: list[int] = Field(min_length=1)
    """`raw_events.id`。空的进不了这个模型,也就进不了库(铁律 5)。"""

    confidence: float = Field(ge=0.0, le=1.0)
    trust: Trust
    """依据的素材可不可信。**只要有一条依据是外部的,整条就是外部的** ——
    仓储据此封顶 0.6。取"最不可信的那条"是唯一安全的取法。"""


class MemoryInput(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    events: list[StoredEvent]
    own_identifiers: list[str] = Field(default_factory=list)
    """用户本人的邮箱 / 企微 userid。**用来把自己排除掉。**

    不排除的话,每封邮件的收件人都是你,记忆里很快会出现一个叫你自己的实体,
    然后"我这周答应了谁什么事"里会冒出你答应了你自己。
    """


class MemoryOutput(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list)
    sightings: list[EntitySighting] = Field(default_factory=list)
    dropped_ungrounded: int = 0
    """指不回真实事件而被丢掉的条数。数量一多说明 prompt 该改了。"""

    considered_events: int = 0


@dataclass(frozen=True)
class MemoryResult:
    output: MemoryOutput
    llm_fields_sent: list[str]
    prompt_tokens: int | None
    completion_tokens: int | None


class MemoryFailed(RuntimeError):
    """抽取整体失败。**不是"这次没抽到东西"** —— 那是正常的,返回空结果即可。"""


def _own_key(value: str) -> str:
    """把"用户本人的标识符"归一成可比的键。

    邮箱、企微 userid 走的是同一套归一化(去空白 + 小写),所以这里借
    `AliasType.EMAIL` 那条分支就够,不必让调用方逐个说明类型。
    """
    return normalize_alias(value, AliasType.EMAIL)


def _kind_for(role: PartyRole) -> EntityKind:
    return EntityKind.MERCHANT if role in _MERCHANT_ROLES else EntityKind.PERSON


def sightings_from(
    stored: Sequence[StoredEvent], *, own_identifiers: Sequence[str] = ()
) -> list[EntitySighting]:
    """从 `parties` 里读出实体。**这一步不调模型。**

    `card_last4` 类型的标识符会被丢掉(只留名字):它标识的是一张卡不是一个人,
    详见 `alias_type_for_identifier`。
    """
    own = {_own_key(v) for v in own_identifiers if v.strip()}
    seen: set[tuple[int, str, str, str | None]] = set()
    result: list[EntitySighting] = []

    for item in stored:
        for party in item.event.parties:
            sighting = _sighting_from_party(party, item)
            if sighting is None:
                continue
            if sighting.identifier and _own_key(sighting.identifier) in own:
                continue
            # 同一封邮件里同一个人出现在 to 和 cc 各一次,是一次目击不是两次
            key = (item.event_id, sighting.kind.value, sighting.name, sighting.identifier)
            if key in seen:
                continue
            seen.add(key)
            result.append(sighting)
    return result


def _sighting_from_party(party: Party, item: StoredEvent) -> EntitySighting | None:
    name = party.display_name.strip()
    identifier = party.identifier
    alias_type = (
        alias_type_for_identifier(party.identifier_type)
        if identifier and party.identifier_type
        else None
    )
    if identifier and alias_type is None:
        # 映射不过来的标识符(卡号后四位)只丢标识符,名字还留着
        identifier = None

    if not name:
        # 没有显示名的参与方就拿标识符当名字:邮件里常见 `<a@b.com>` 不带姓名,
        # 那时候邮箱本身就是我们对这个人唯一的称呼
        name = identifier or ""
    if not name:
        return None

    return EntitySighting(
        kind=_kind_for(party.role),
        name=name,
        identifier=identifier,
        identifier_type=alias_type,
        seen_at=item.event.occurred_at,
        event_id=item.event_id,
    )


def build_blocks(
    stored: Sequence[StoredEvent], *, max_events: int = MAX_EVENTS
) -> list[ExternalBlock]:
    """把事件包成隔离块。**正文之外的字段用规则单独给**(铁律 9 + R12)。"""
    blocks: list[ExternalBlock] = []
    ordered = sorted(stored, key=lambda s: s.event.occurred_at, reverse=True)[:max_events]
    for item in ordered:
        event = item.event
        fields = {"标题": event.title, "时间": event.occurred_at.isoformat()}
        senders = [p.display_name for p in event.parties if p.role is PartyRole.FROM]
        if senders:
            fields["发件人"] = "、".join(senders)
        blocks.append(
            ExternalBlock(
                source=event.external_ref.source,
                external_id=event.external_ref.external_id,
                text=event.body or "",
                fields=fields,
            )
        )
    return blocks


def _trust_of(events: Sequence[NormalizedEvent]) -> Trust:
    return (
        Trust.USER_INPUT
        if events and all(e.trust is Trust.USER_INPUT for e in events)
        else Trust.EXTERNAL
    )


def _parse(payload: object, index: dict[str, StoredEvent]) -> tuple[list[ExtractedFact], int]:
    if not isinstance(payload, dict):
        raise MemoryFailed(f"模型返回的顶层不是对象:{type(payload).__name__}")

    facts: list[ExtractedFact] = []
    dropped = 0

    for raw in payload.get("facts") or []:
        if not isinstance(raw, dict):
            dropped += 1
            continue

        refs = [str(r) for r in (raw.get("refs") or [])]
        sources = [index[key] for r in refs if (key := normalize_ref(r)) in index]
        if not sources:
            # 摘要在这里会保留并标 unverified,记忆不行:没有来源的记忆不许落库
            log.info("丢弃指不回事件的事实:refs=%s", refs)
            dropped += 1
            continue

        try:
            facts.append(
                ExtractedFact(
                    statement=str(raw.get("statement", "")).strip(),
                    provenance=sorted({s.event_id for s in sources}),
                    confidence=_clamp(raw.get("confidence")),
                    trust=_trust_of([s.event for s in sources]),
                )
            )
        except ValidationError:
            # 单条不合格(空句子、超长复述)不该毁掉整次抽取
            dropped += 1

    return facts, dropped


def _clamp(value: object) -> float:
    """把模型给的置信度收进 [0, 1]。

    收而不是拒:越界值说明模型没按格式来,而这条事实本身可能是对的。
    真正决定它有多可信的是来源 —— 外部来源在仓储那一层照样被压到 0.6。
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5
    if number < 0.0 or number > 1.0:
        log.info("模型给出越界的 confidence=%s,收进 [0,1]", number)
    return min(1.0, max(0.0, number))


@agent(
    name="memory",
    inputs=MemoryInput,
    tools=[],  # 事件由调度层取好传进来,写库也由它做。这个 agent 只产出结构
    output_schema=MemoryOutput,
    on_uncertain=OnUncertain.DO_NOTHING,
    evalset="evals/memory.jsonl",
)
def extract(payload: MemoryInput, *, llm: LLMClient, max_events: int = MAX_EVENTS) -> MemoryResult:
    sightings = sightings_from(payload.events, own_identifiers=payload.own_identifiers)
    blocks = build_blocks(payload.events, max_events=max_events)

    if not blocks:
        # 没素材就别调模型:让它对着空 prompt 抽事实,抽出来的一定是编的
        return MemoryResult(
            output=MemoryOutput(sightings=sightings),
            llm_fields_sent=[],
            prompt_tokens=None,
            completion_tokens=None,
        )

    messages = build_messages(task=_TASK, blocks=blocks, max_chars_per_block=MAX_CHARS_PER_EVENT)
    response = llm.chat(messages)

    try:
        parsed = response.as_json()
    except LLMBadResponse as exc:
        raise MemoryFailed(f"抽取结果解析失败:{exc}") from exc

    sent = {b.external_id for b in blocks}
    index = {
        normalize_ref(item.event.external_ref.external_id): item
        for item in payload.events
        if item.event.external_ref.external_id in sent
    }
    facts, dropped = _parse(parsed, index)

    return MemoryResult(
        output=MemoryOutput(
            facts=facts,
            sightings=sightings,
            dropped_ungrounded=dropped,
            considered_events=len(blocks),
        ),
        llm_fields_sent=fields_sent(blocks),
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
    )
