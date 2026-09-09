"""记账 job —— 四层防误判的出口。

    raw_events(kind=transaction)
        → bookkeeper agent(第 3、4 层)
        → 归类:merchant_rules 优先,模型只兜长尾(ADR-008)
        → 直接入账的 → 过网关调 txn.record(L2)
        → 拿不准的   → pending_confirmations
        → 确定不是交易的 → 丢弃,不进队列

和日程提取一样,它**不采集**,只读已经落库的事件,自己认领窗口。

**丢弃这条路是这个 job 独有的。** 日程那边只有"建"和"待确认"两种去向,
因为一条群消息即使不是日程,也没有第三种处理方式。记账不同:营销短信
每天都有,把它们塞进待确认队列会把队列淹掉,而淹掉的队列等于没有队列 ——
用户会在某一天不再打开它,然后真正需要确认的那笔也一起看不见了。

**一个窗口里的事件按 40 条一批送模型**,不是只送最前面 40 条。
后者是这个文件原来的行为:一天超过 40 条交易时,较旧的那些既没入账
也没进待确认,而窗口照样标成 succeeded —— 于是它们再也不会被读第二次。
分批安全的前提是跨渠道合并与去重都在 `txn.record` 里按库上的唯一键做,
不依赖"同一批里能不能看见对方"。

**入账失败会让整窗失败。** 也是原来写错的一处:那里的注释说"明天还会被
判一次",而窗口标成功之后不会有明天。整窗重跑靠
`UNIQUE (user_id, source_event_id)` 兜底,已记成的会被判成 duplicate。

**这个 job 是 `merchant_rules.remember()` 唯一的调用方。** 沉淀发生在
入账成功之后,而且只沉淀真实商户名 —— 代收机构名被 `remember()` 自己挡掉
(ADR-008)。实时这一遍多数会被挡住,那是常态:真正的沉淀要等第 6 片
月度账单回填出真商户之后。所以**现在看 `llm_share` 不会下降,不是 bug**;
它要从第 6 片之后开始下降,那时候不降才是。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from lifein.agents.bookkeeper import (
    MAX_EVENTS,
    BookkeeperFailed,
    BookkeeperInput,
    JudgedTransaction,
    Route,
    judge,
)
from lifein.alerts import Alerter
from lifein.governance.audit import ToolCallRecord
from lifein.governance.gateway import CallContext, Gateway
from lifein.governance.registry import ToolLevel
from lifein.llm.client import LLMClient
from lifein.models.normalized import PartyRole, Trust
from lifein.repos import job_runs, merchant_rules, pending, raw_events
from lifein.repos.tool_calls import PostgresAuditSink, record_tool_call
from lifein.repos.transactions import TxnKind

log = logging.getLogger(__name__)

JOB_NAME = "bookkeeping"
AGENT = "bookkeeper"
TARGET_TABLE = "transactions"
TOOL = "txn.record"

MAX_EVENTS_PER_WINDOW = 400
"""一个窗口最多读几条交易事件。

**它和 `MAX_EVENTS`(模型一批 40 条)是两个不同的上限**,别合并:
超过 40 条会分批送模型,一条都不会漏;超过 400 条才是真的读不完 ——
而那时会告警,不会静默截断(原来那一版就是静默的)。

个人账本一天 400 笔已经远超正常,真出现多半是采集器在重放,
而那种情况下"读完"没有意义,"喊一声"才有。
"""

BACKLOG_ALERT_AT = 20
"""待确认积压到多少条就告警。和日程那边同一个数,理由也一样:
不是"处理不完",是判据太保守的信号。"""

DEFAULT_CHANNEL = "notification"

GatewayFactory = Callable[[str, Session], Gateway]


def default_gateway(user_id: str, session: Session) -> Gateway:
    """审计沿用调用方的事务:业务回滚时审计跟着回滚。"""
    return Gateway(PostgresAuditSink(user_id, session))


@dataclass(frozen=True)
class BookkeepingDeps:
    llm: LLMClient
    alerter: Alerter
    gateway_factory: GatewayFactory = default_gateway


@dataclass
class BookkeepingResult:
    window_start: datetime
    window_end: datetime
    skipped: bool = False
    no_events: bool = False

    events_considered: int = 0
    recorded: int = 0
    """真的新增了一笔。**合并进已有那笔的不算** —— 算了的话日志上的入账条数
    会比账本上的笔数多,而对账时你会以为丢了几笔。"""

    merged: int = 0
    duplicates: int = 0
    queued: int = 0
    discarded: int = 0
    """确定不是交易的(营销、待支付提醒)。**这个数应该是最大的那个。**"""

    dropped_ungrounded: int = 0
    failed_review: int = 0
    failed_record: int = 0
    """`txn.record` 抛了异常的笔数。**大于 0 就让整窗算失败** ——
    见 `_record` 里那段注释:窗口标成功等于这些事件被永久跳过。"""

    categorized_by_rule: int = 0
    categorized_by_llm: int = 0
    rules_learned: int = 0
    pending_backlog: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def llm_share(self) -> float | None:
        """ADR-008 的监控指标。**现在不会下降,见模块开头。**"""
        return merchant_rules.llm_share(
            {"by_rule": self.categorized_by_rule, "by_llm": self.categorized_by_llm}
        )

    def as_stats(self) -> dict:
        return {
            "events_considered": self.events_considered,
            "recorded": self.recorded,
            "merged": self.merged,
            "duplicates": self.duplicates,
            "queued": self.queued,
            "discarded": self.discarded,
            "dropped_ungrounded": self.dropped_ungrounded,
            "failed_review": self.failed_review,
            "failed_record": self.failed_record,
            "categorized_by_rule": self.categorized_by_rule,
            "categorized_by_llm": self.categorized_by_llm,
            "rules_learned": self.rules_learned,
            "llm_share": self.llm_share,
            "pending_backlog": self.pending_backlog,
            "no_events": self.no_events,
        }


def run_once(
    user_id: str,
    session: Session,
    *,
    deps: BookkeepingDeps,
    now: datetime,
    window: timedelta = timedelta(days=1),
) -> list[BookkeepingResult]:
    windows = job_runs.windows_to_run(user_id, session, job_name=JOB_NAME, now=now, length=window)
    return [
        _run_window(user_id, session, deps=deps, start=start, end=end, now=now)
        for start, end in windows
    ]


def _run_window(
    user_id: str,
    session: Session,
    *,
    deps: BookkeepingDeps,
    start: datetime,
    end: datetime,
    now: datetime,
) -> BookkeepingResult:
    result = BookkeepingResult(window_start=start, window_end=end)

    if not job_runs.claim_window(
        user_id, session, job_name=JOB_NAME, window_start=start, window_end=end, now=now
    ):
        result.skipped = True
        return result

    try:
        _book_window(user_id, session, deps=deps, start=start, end=end, now=now, result=result)
    except BookkeeperFailed as exc:
        result.error = str(exc)
        deps.alerter.alert("记账判定失败", str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("记账 job 异常")
        result.error = f"{type(exc).__name__}: {exc}"
        deps.alerter.alert("记账异常终止", result.error)

    job_runs.finish_window(
        user_id,
        session,
        job_name=JOB_NAME,
        window_start=start,
        status="failed" if result.error else "succeeded",
        stats=result.as_stats(),
        error=result.error,
    )
    return result


def _book_window(
    user_id: str,
    session: Session,
    *,
    deps: BookkeepingDeps,
    start: datetime,
    end: datetime,
    now: datetime,
    result: BookkeepingResult,
) -> None:
    stored = raw_events.fetch_transactions_between(
        user_id, session, start=start, end=end, limit=MAX_EVENTS_PER_WINDOW
    )
    if not stored:
        log.info("窗口 %s 内没有交易事件", start.isoformat())
        result.no_events = True
        result.pending_backlog = pending.count_pending(user_id, session, now=now)
        return

    if len(stored) >= MAX_EVENTS_PER_WINDOW:
        # 取到上限就说明可能还有没取到的。**这一条必须喊出来** ——
        # 静默截断的症状是"账本上这天少了几笔",而那要到月底对账才看得出来
        message = f"一个窗口里取到 {len(stored)} 条交易事件,已到上限,更早的没读"
        log.warning(message)
        result.warnings.append(message)
        deps.alerter.alert("记账窗口事件过多", message)

    gateway = deps.gateway_factory(user_id, session)

    # **分批送模型,不是只送最前面 40 条。**
    # 原来这里一次性把整窗事件交给 `judge`,而它内部 `[:MAX_EVENTS]` 截断:
    # 一天超过 40 条时,较旧的那些既没入账也没进待确认,窗口却照样标成功 ——
    # 于是它们再也不会被读一次。分批之后每一条都会被判到,代价是多几次模型调用
    for batch in _batches(stored, size=MAX_EVENTS):
        _judge_and_apply(
            user_id, session, gateway, batch, deps=deps, now=now, result=result
        )

    if result.failed_record:
        # 落在这里而不是 `_record` 里直接抛:剩下的那些笔仍然值得处理一遍,
        # 处理完了再把整窗标成失败,下次重跑时它们会被判成 duplicate
        result.error = f"{result.failed_record} 笔入账失败,这一窗要重跑"
        deps.alerter.alert("入账失败", result.error)

    result.pending_backlog = pending.count_pending(user_id, session, now=now)
    if result.pending_backlog >= BACKLOG_ALERT_AT:
        message = f"待确认积压 {result.pending_backlog} 条,判据可能太保守了"
        result.warnings.append(message)
        deps.alerter.alert("待确认队列积压", message)


def _batches(
    stored: list[raw_events.StoredEvent], *, size: int
) -> Iterator[list[raw_events.StoredEvent]]:
    """按模型一次能吃下的条数切开。

    **切的是"送给模型的批",不是"业务上的组"** —— 跨渠道合并、去重全都在
    `txn.record` 里按库上的唯一键做,不依赖同一批里能不能看见对方。
    所以怎么切都不影响结果,只影响调用几次。
    """
    for i in range(0, len(stored), size):
        yield stored[i : i + size]


def _judge_and_apply(
    user_id: str,
    session: Session,
    gateway: Gateway,
    batch: list[raw_events.StoredEvent],
    *,
    deps: BookkeepingDeps,
    now: datetime,
    result: BookkeepingResult,
) -> None:
    """一批:判定 + 落账。计数累加进同一个 `result`。"""
    judged = judge(BookkeeperInput(events=batch), llm=deps.llm)
    output = judged.output
    result.events_considered += output.considered_events
    result.dropped_ungrounded += output.dropped_ungrounded
    result.failed_review += output.failed_review

    record_tool_call(
        user_id,
        session,
        ToolCallRecord(
            user_id=user_id,
            agent=AGENT,
            tool_name="llm.chat",
            level=ToolLevel.L1,
            args_digest={"events": {"type": "list", "len": len(batch)}},
            llm_fields_sent=judged.llm_fields_sent,
            result_status="allowed",
            prompt_tokens=judged.prompt_tokens,
            completion_tokens=judged.completion_tokens,
        ),
    )

    by_event = {item.event_id: item for item in batch}
    for item in output.items:
        source = by_event.get(item.event_id)
        if source is None:
            # 判定指回了一个这一批里没有的事件。理论上进不来(index 就是从
            # 这批事件建的),真出现说明有 bug,而记一笔来源不明的钱最糟
            result.dropped_ungrounded += 1
            continue

        if item.route is Route.DISCARD:
            result.discarded += 1
        elif item.route is Route.PENDING:
            _queue(user_id, session, item, source, now=now, result=result)
        else:
            _record(user_id, session, gateway, item, source, result=result)


def _record(
    user_id: str,
    session: Session,
    gateway: Gateway,
    item: JudgedTransaction,
    source: raw_events.StoredEvent,
    *,
    result: BookkeepingResult,
) -> None:
    """入账。**归类在这里做,不在 agent 里** —— 规则表要查库,而 agent 不碰库。"""
    merchant = _merchant_of(source)
    decided = merchant_rules.categorize(
        user_id, session, merchant=merchant, llm_category=item.category
    )
    if decided.by_rule:
        result.categorized_by_rule += 1
    elif decided.category is not None:
        result.categorized_by_llm += 1

    ctx = CallContext(
        user_id=user_id,
        agent=AGENT,
        # 如实记来源可信度:这笔钱是被一条外部通知触发记下的
        trust=source.event.trust or Trust.EXTERNAL,
        source_event_id=item.event_id,
        session=session,
    )
    try:
        value = gateway.call(ctx, TOOL, _args_for(item, source, category=decided.category))
    except Exception as exc:  # noqa: BLE001
        # **这里原来写的是"这一笔明天还会被判一次",那句是假的。**
        # 事件确实还在,但窗口会被标成 succeeded,而"该跑哪些窗口"只从
        # 最后一个成功窗口往后切 —— 这一窗再也不会被读第二次,那笔钱就没了。
        #
        # 所以记一个 `failed_record`,由 `_book_window` 把整窗标成 failed。
        # 整窗重跑是安全的:`transactions` 上有 UNIQUE (user_id, source_event_id),
        # 已经记成的那些会被判成 duplicate,不会记两遍。
        log.warning("入账失败,这一窗会重跑:%s", exc)
        result.warnings.append(f"入账失败:{exc}")
        result.failed_record += 1
        return

    if value["duplicate"]:
        result.duplicates += 1
        return
    if value["merged_into"] is not None:
        result.merged += 1
        return

    result.recorded += 1
    # 沉淀放在入账成功之后:没记成的那笔连商户名都不该信
    if decided.category and not decided.by_rule:
        learned = merchant_rules.remember(
            user_id, session, merchant=merchant, category=decided.category
        )
        if learned is not None:
            result.rules_learned += 1


def _queue(
    user_id: str,
    session: Session,
    item: JudgedTransaction,
    source: raw_events.StoredEvent,
    *,
    now: datetime,
    result: BookkeepingResult,
) -> None:
    """进待确认队列。

    `payload` 存的是**照它就能入账的形状**,不是给人看的描述 ——
    确认之后由 06 §2.7 那条"同一个事务"的路径原样写进 `transactions`。
    分类留成 agent 给的那个:用户确认时看到一个具体分类才好改,
    给个空的等于把归类这件事也推给他。
    """
    pending.enqueue(
        user_id,
        session,
        agent=AGENT,
        kind=pending.PendingKind.TRANSACTION,
        target_table=TARGET_TABLE,
        payload=_args_for(item, source, category=item.category),
        reason=pending.PendingReason(item.reason or "low_confidence"),
        confidence=item.confidence,
        source_event_id=item.event_id,
        now=now,
    )
    result.queued += 1


def _args_for(
    item: JudgedTransaction, source: raw_events.StoredEvent, *, category: str | None
) -> dict:
    """凑 `txn.record` 的入参。**金额、方向、卡号一律取归一化那份**,
    不取模型说的 —— 模型这一轮只回答了"这是哪一类资金变动"(铁律 9)。
    """
    event = source.event
    amount = event.amount
    kind = item.kind or TxnKind.EXPENSE
    return {
        "occurred_at": event.occurred_at.isoformat(),
        "amount": str(amount.value),
        "currency": amount.currency,
        "direction": amount.direction.value,
        "kind": kind.value,
        "channel": _channel_of(source),
        "source_event_id": item.event_id,
        "confidence": item.confidence,
        "merchant_raw": _merchant_of(source),
        "category": category if kind is TxnKind.EXPENSE else None,
        "account_hint": _account_hint_of(source),
    }


def _channel_of(source: raw_events.StoredEvent) -> str:
    """哪条渠道送来的。**跨渠道合并靠它区分** —— 取不到就退回一个统一值,
    那样合并不会误判成"同一条渠道来了两次"而放弃合并。
    """
    raw = source.raw or {}
    channel = raw.get("channel")
    return channel if isinstance(channel, str) and channel else DEFAULT_CHANNEL


def _account_hint_of(source: raw_events.StoredEvent) -> str | None:
    parsed = (source.raw or {}).get("parsed")
    if not isinstance(parsed, dict):
        return None
    hint = parsed.get("account_hint")
    return hint if isinstance(hint, str) and hint else None


def _merchant_of(source: raw_events.StoredEvent) -> str | None:
    for party in source.event.parties:
        if party.role is PartyRole.MERCHANT:
            return party.display_name
    parsed = (source.raw or {}).get("parsed")
    if isinstance(parsed, dict):
        merchant = parsed.get("merchant_raw")
        if isinstance(merchant, str) and merchant:
            return merchant
    return None
