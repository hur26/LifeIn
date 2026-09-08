"""记账 agent —— 四层防误判里的第 3、4 层
([ADR-012](../../docs/04-tech-decisions.md#adr-012--账单采集以实时通知为主导出账单为辅))。

四层依次是:源头白名单 → 5 分钟去重窗口 → **LLM 判定是不是真实支出** →
**代码复核**。前两层在采集与仓储那边,这个模块是后两层。

**03 的验收标准里,这个 agent 对应的那条不设百分比**:

> **误记率 = 0**:一个月内没有任何一笔"待支付提醒""信用卡还款""退款"
> 被错记成支出。这一条不设阈值 —— 错记会直接摧毁对账本的信任。

所以这里的取向和别的 agent 不同:**它宁可什么都不入账**。
拿不准的一律进待确认(铁律 7),而"拿不准"的判据很宽。

**金额、卡号、方向不经过模型**(铁律 9)。它们在采集那一步就用正则抠好了,
模型只回答一件事:**这段话描述的是哪一类资金变动**。这么切分有三个好处 ——
最敏感的字段不出服务器(R12)、模型答错了也改不了金额、
以及第 4 层能拿规则抠出来的值去复核模型的结论。

**第 4 层复核三件事**,任何一件不过就进待确认:

1. 金额能不能在原文里**逐字**找到 —— 模型如果"顺手"改了小数点,这一条会拦住
2. 分类在不在枚举内 —— 自由文本的分类会让报表长出无穷多个类目
3. 置信度够不够(`TXN_MIN_CONFIDENCE`,默认 0.8)
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from lifein.agents.contract import OnUncertain, agent
from lifein.llm.client import LLMBadResponse, LLMClient
from lifein.llm.prompt import ExternalBlock, build_messages, fields_sent, normalize_ref
from lifein.models.normalized import PartyRole
from lifein.repos.raw_events import StoredEvent
from lifein.repos.transactions import CATEGORIES, TxnKind

log = logging.getLogger(__name__)

MAX_EVENTS = 40
MAX_CHARS_PER_EVENT = 600
"""交易通知都很短。给得比日程少,是因为长文本里多半是营销话术,
而它们只会把模型往"这是一笔消费"上带。"""

DEFAULT_MIN_CONFIDENCE = 0.8
"""低于它一律进待确认(07 的 `TXN_MIN_CONFIDENCE`)。

比日程那边的 0.7 严,因为代价不对称:待办多一条划掉就行,
账本上多一笔会让每一个统计数字都不可信。
"""


class Judgment(StrEnum):
    """模型能给的答案。**比 `TxnKind` 多两个** —— 那两个的意思是"根本不是交易"。"""

    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"
    REFUND = "refund"
    REPAYMENT = "repayment"

    PENDING_PAYMENT = "pending_payment"
    """待支付提醒("您有一笔账单待支付")。**钱还没动。**"""

    MARKETING = "marketing"
    """营销("消费满 100 减 20")。里面的数字不是你花的钱。"""

    @property
    def as_txn_kind(self) -> TxnKind | None:
        """能落账的那五种。后两种返回 None —— 它们不该进 `transactions`。"""
        try:
            return TxnKind(self.value)
        except ValueError:
            return None


class Route(StrEnum):
    DIRECT = "direct"
    """四层全过,直接入账。"""

    PENDING = "pending"
    """进待确认队列。**这是"拿不准"的形态,不是失败。**"""

    DISCARD = "discard"
    """模型有把握地说"这不是一笔交易"(营销、待支付提醒)。

    **丢弃而不是进队列**:它们每天都有,进队列会把队列淹掉,
    而淹掉的队列等于没有队列。
    """


class JudgedTransaction(BaseModel):
    event_id: int
    judgment: Judgment
    kind: TxnKind | None
    category: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    route: Route
    reason: str = ""
    """走待确认的原因,直接写进 `pending_confirmations.reason`。"""


class BookkeeperInput(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    events: list[StoredEvent]
    min_confidence: float = DEFAULT_MIN_CONFIDENCE


class BookkeeperOutput(BaseModel):
    items: list[JudgedTransaction] = Field(default_factory=list)
    considered_events: int = 0
    dropped_ungrounded: int = 0
    """模型引用了不存在的素材。和别的 agent 同一条规矩:指不回来源就不要。"""

    failed_review: int = 0
    """第 4 层复核没过的条数。**这个数字长期不降说明 prompt 有问题**,
    而不是"复核在起作用" —— 起作用的复核应该越来越少拦到东西。"""


@dataclass(frozen=True)
class BookkeeperResult:
    output: BookkeeperOutput
    llm_fields_sent: list[str]
    prompt_tokens: int | None
    completion_tokens: int | None


class BookkeeperFailed(RuntimeError):
    """这一轮没产出结果。调度层要告警,不要静默跳过。"""


_TASK = (
    "你是一个记账助手。下面每条素材是一条**银行或支付通知**,"
    "金额和卡号已经由程序抠出来了,**你不需要也不要重复它们**。\n\n"
    "你只回答一件事:这段话描述的是哪一类资金变动。\n\n"
    "- expense:真实的消费支出\n"
    "- income:收入(工资、退税、利息)\n"
    "- transfer:转账(钱换了个地方,不是花掉)\n"
    "- refund:退款\n"
    "- repayment:信用卡还款(**不是消费** —— 消费那一刻已经记过一次)\n"
    "- pending_payment:待支付提醒,钱还没动\n"
    "- marketing:营销、优惠、额度调整,里面的数字不是你花的钱\n\n"
    f"是 expense 的再给一个分类,只能从这些里选:{'、'.join(CATEGORIES)}\n"
    "其余类型的分类填 null。\n\n"
    "输出严格的 JSON,不要加解释文字:\n"
    '{"items": [{"ref": "这条素材的 id",\n'
    '            "judgment": "上面七个之一",\n'
    '            "category": "分类或 null",\n'
    '            "confidence": 0 到 1 之间的小数}]}\n\n'
    "**拿不准就把 confidence 写低**,不要猜一个类型。"
    "记错一笔账比漏记一笔糟得多。"
)


def build_blocks(
    stored: Sequence[StoredEvent], *, max_events: int = MAX_EVENTS
) -> list[ExternalBlock]:
    """把交易事件包成隔离块。

    **金额、卡号、方向作为结构化字段单独给**,不混在正文里(铁律 9 与 06 §5)——
    模型看得到它们才能判断类型,但它改不了它们:落账用的是规则抠出来的值。
    """
    blocks: list[ExternalBlock] = []
    for item in sorted(stored, key=lambda s: s.event.occurred_at, reverse=True)[:max_events]:
        event = item.event
        fields = {"时间": event.occurred_at.isoformat(), "标题": event.title}
        if event.amount is not None:
            fields["金额"] = f"{event.amount.value} {event.amount.currency}"
            fields["方向"] = event.amount.direction.value
        merchants = [p.display_name for p in event.parties if p.role is PartyRole.MERCHANT]
        if merchants:
            fields["商户"] = "、".join(merchants)

        blocks.append(
            ExternalBlock(
                source=event.external_ref.source,
                external_id=event.external_ref.external_id,
                text=event.body or "",
                fields=fields,
            )
        )
    return blocks


@agent(
    name="bookkeeper",
    inputs=BookkeeperInput,
    tools=["txn.record"],
    output_schema=BookkeeperOutput,
    on_uncertain=OnUncertain.PENDING_CONFIRMATION,
    evalset="evals/bookkeeper.jsonl",
)
def judge(payload: BookkeeperInput, *, llm: LLMClient) -> BookkeeperResult:
    """第 3 层(模型判定)+ 第 4 层(代码复核)。"""
    transactions = [item for item in payload.events if item.event.amount is not None]
    if not transactions:
        return BookkeeperResult(
            output=BookkeeperOutput(),
            llm_fields_sent=[],
            prompt_tokens=None,
            completion_tokens=None,
        )

    blocks = build_blocks(transactions)
    messages = build_messages(task=_TASK, blocks=blocks, max_chars_per_block=MAX_CHARS_PER_EVENT)
    response = llm.chat(messages)

    try:
        parsed = response.as_json()
    except LLMBadResponse as exc:
        raise BookkeeperFailed(f"记账判定解析失败:{exc}") from exc

    index = {normalize_ref(item.event.external_ref.external_id): item for item in transactions}
    output = _parse(parsed, index, min_confidence=payload.min_confidence)
    output.considered_events = len(transactions)

    return BookkeeperResult(
        output=output,
        llm_fields_sent=fields_sent(blocks),
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
    )


def _parse(
    payload: object, index: dict[str, StoredEvent], *, min_confidence: float
) -> BookkeeperOutput:
    if not isinstance(payload, dict):
        raise BookkeeperFailed(f"模型返回的顶层不是对象:{type(payload).__name__}")

    output = BookkeeperOutput()
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict):
            output.dropped_ungrounded += 1
            continue

        key = normalize_ref(str(raw.get("ref", "")))
        stored = index.get(key)
        if stored is None:
            # 指不回素材 = 模型编了一条通知。和记忆、日程那边同一条规矩
            log.info("丢弃指不回事件的判定:ref=%s", raw.get("ref"))
            output.dropped_ungrounded += 1
            continue

        judged = _review(stored, raw, min_confidence=min_confidence)
        if judged.route is Route.PENDING and judged.reason == "check_failed":
            output.failed_review += 1
        output.items.append(judged)

    return output


def _review(
    stored: StoredEvent, raw: dict, *, min_confidence: float
) -> JudgedTransaction:
    """**第 4 层:代码复核。** 模型说了什么,和它说得对不对,是两件事。"""
    event_id = stored.event_id
    confidence = _clamp(raw.get("confidence"))

    judgment = _judgment_of(raw.get("judgment"))
    if judgment is None:
        # 模型给了枚举外的答案。**不猜一个最接近的** —— 那正是错记的来源
        return JudgedTransaction(
            event_id=event_id,
            judgment=Judgment.EXPENSE,
            kind=None,
            category=None,
            confidence=confidence,
            route=Route.PENDING,
            reason="check_failed",
        )

    kind = judgment.as_txn_kind
    if kind is None:
        # 营销、待支付提醒:模型有把握就丢,没把握就进队列
        route = Route.DISCARD if confidence >= min_confidence else Route.PENDING
        return JudgedTransaction(
            event_id=event_id,
            judgment=judgment,
            kind=None,
            category=None,
            confidence=confidence,
            route=route,
            reason="" if route is Route.DISCARD else "low_confidence",
        )

    category = _category_of(raw.get("category"), kind=kind)
    amount_ok = _amount_is_verbatim(stored)

    if not amount_ok or (kind is TxnKind.EXPENSE and category is None):
        return JudgedTransaction(
            event_id=event_id,
            judgment=judgment,
            kind=kind,
            category=category,
            confidence=confidence,
            route=Route.PENDING,
            reason="check_failed",
        )

    if confidence < min_confidence:
        return JudgedTransaction(
            event_id=event_id,
            judgment=judgment,
            kind=kind,
            category=category,
            confidence=confidence,
            route=Route.PENDING,
            reason="low_confidence",
        )

    return JudgedTransaction(
        event_id=event_id,
        judgment=judgment,
        kind=kind,
        category=category,
        confidence=confidence,
        route=Route.DIRECT,
    )


def _amount_is_verbatim(stored: StoredEvent) -> bool:
    """**金额必须能在原文里逐字找到。**

    正文已经被脱敏(R10)的那些没法比对 —— 那时认这一条通过:
    金额本来就不是模型给的,它来自采集那一步的正则,而那一步的输入正是原文。
    这条复核挡的是"归一化之后有人改过金额",不是"原文还在不在"。
    """
    event = stored.event
    if event.amount is None:
        return False
    body = event.body
    if not body:
        return True

    value: Decimal = event.amount.value
    # 38.50 在原文里可能写成 38.5 或 38.50,两种都算逐字
    candidates = {f"{value}", f"{value:f}".rstrip("0").rstrip("."), f"{value:,}"}
    return any(candidate and candidate in body for candidate in candidates)


def _judgment_of(value: object) -> Judgment | None:
    try:
        return Judgment(str(value).strip().lower())
    except ValueError:
        return None


def _category_of(value: object, *, kind: TxnKind) -> str | None:
    """分类必须在枚举内。**不在就返回 None**,由调用方判成复核失败。"""
    if kind is not TxnKind.EXPENSE:
        # 只有支出需要分类:给收入分个"餐饮"只会让报表更难看
        return None
    text = str(value).strip() if value is not None else ""
    return text if text in CATEGORIES else None


def _clamp(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))
