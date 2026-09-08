"""日程与待办提取 job。

    raw_events → planner agent → 够有把握的 → 过网关调 todo.create(L2)
                              └→ 拿不准的  → pending_confirmations

和记忆抽取一样,它**不采集**,只读已经落库的事件,自己认领窗口。
三个 job 各跑各的,谁挂了都不连累另外两个。

**这是本项目第一条会写东西的自动链路。** 前面所有 job 要么只读(摘要、问答),
要么只写自己的记忆表(记忆抽取);这一条会往你的待办列表里放东西,
确认之后还会进你的日历。所以两件事在这里格外重要:

**每一次写入都过网关。** 不是形式主义:`tool_calls` 里那条带 `rollback_info`
的记录,是"这条待办哪来的、怎么撤"唯一的答案。绕过网关直接调仓储也能写进去,
但那样写出来的东西没人能解释。

**队列积压要能被看见。** `pending_backlog` 长期只涨不落,说明判据太保守 ——
那不是安全,是把活全推给了用户,而用户会在某一天不再看那个列表。
它和"误报进日历"是同一枚硬币的两面,只盯一面必然把另一面做坏。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from lifein.agents.planner import (
    ItemKind,
    PlannerFailed,
    PlannerInput,
    Route,
    extract,
    trust_of,
)
from lifein.alerts import Alerter
from lifein.governance.audit import ToolCallRecord
from lifein.governance.gateway import CallContext, Gateway
from lifein.governance.registry import ToolLevel
from lifein.llm.client import LLMClient
from lifein.repos import job_runs, pending, raw_events
from lifein.repos.tool_calls import PostgresAuditSink, record_tool_call

log = logging.getLogger(__name__)

JOB_NAME = "plan_extract"
AGENT = "planner"
TARGET_TABLE = "todos"

BACKLOG_ALERT_AT = 20
"""待确认积压到多少条就告警。

不是"太多处理不完"的意思 —— 是判据太保守的信号。一个每天给你堆三条待确认
的系统,和一个每天误报三条的系统,最后都会被关掉。
"""

GatewayFactory = Callable[[str, Session], Gateway]


def default_gateway(user_id: str, session: Session) -> Gateway:
    """审计沿用调用方的事务:业务回滚时审计跟着回滚。"""
    return Gateway(PostgresAuditSink(user_id, session))


@dataclass(frozen=True)
class PlanDeps:
    llm: LLMClient
    alerter: Alerter
    gateway_factory: GatewayFactory = default_gateway


@dataclass
class PlanResult:
    window_start: datetime
    window_end: datetime
    skipped: bool = False
    no_events: bool = False

    events_considered: int = 0
    created: int = 0
    """直接建出来的条数。**目前只可能是待办** —— 日程一律走待确认。"""

    queued: int = 0
    dropped_ungrounded: int = 0
    dropped_past: int = 0
    expired: int = 0
    pending_backlog: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_stats(self) -> dict:
        return {
            "events_considered": self.events_considered,
            "created": self.created,
            "queued": self.queued,
            "dropped_ungrounded": self.dropped_ungrounded,
            "dropped_past": self.dropped_past,
            "expired": self.expired,
            "pending_backlog": self.pending_backlog,
            "no_events": self.no_events,
        }


def run_once(
    user_id: str,
    session: Session,
    *,
    deps: PlanDeps,
    now: datetime,
    window: timedelta = timedelta(days=1),
) -> list[PlanResult]:
    windows = job_runs.windows_to_run(user_id, session, job_name=JOB_NAME, now=now, length=window)
    return [
        _run_window(user_id, session, deps=deps, start=start, end=end, now=now)
        for start, end in windows
    ]


def _run_window(
    user_id: str,
    session: Session,
    *,
    deps: PlanDeps,
    start: datetime,
    end: datetime,
    now: datetime,
) -> PlanResult:
    result = PlanResult(window_start=start, window_end=end)

    if not job_runs.claim_window(
        user_id, session, job_name=JOB_NAME, window_start=start, window_end=end
    ):
        result.skipped = True
        return result

    try:
        _extract_window(user_id, session, deps=deps, start=start, end=end, now=now, result=result)
    except PlannerFailed as exc:
        result.error = str(exc)
        deps.alerter.alert("日程提取失败", str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("日程提取 job 异常")
        result.error = f"{type(exc).__name__}: {exc}"
        deps.alerter.alert("日程提取异常终止", result.error)

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


def _extract_window(
    user_id: str,
    session: Session,
    *,
    deps: PlanDeps,
    start: datetime,
    end: datetime,
    now: datetime,
    result: PlanResult,
) -> None:
    # 过期清理先做:它和这次提没提到东西无关,而一条三十天前的"周三开会"
    # 留在列表里只会让人越来越不想打开它
    result.expired = pending.expire_overdue(user_id, session, now=now)

    stored = raw_events.fetch_stored_between(user_id, session, start=start, end=end)
    if not stored:
        log.info("窗口 %s 内没有事件,不提取", start.isoformat())
        result.no_events = True
        result.pending_backlog = pending.count_pending(user_id, session, now=now)
        return

    planned = extract(PlannerInput(events=stored, now=now), llm=deps.llm)
    output = planned.output
    result.events_considered = output.considered_events
    result.dropped_ungrounded = output.dropped_ungrounded
    result.dropped_past = output.dropped_past

    record_tool_call(
        user_id,
        session,
        ToolCallRecord(
            user_id=user_id,
            agent=AGENT,
            tool_name="llm.chat",
            level=ToolLevel.L1,
            args_digest={"events": {"type": "list", "len": len(stored)}},
            llm_fields_sent=planned.llm_fields_sent,
            result_status="allowed",
            prompt_tokens=planned.prompt_tokens,
            completion_tokens=planned.completion_tokens,
        ),
    )

    by_event = {item.event_id: item for item in stored}
    gateway = deps.gateway_factory(user_id, session)

    for extracted in output.items:
        sources = [by_event[i] for i in extracted.provenance if i in by_event]
        if extracted.route is Route.DIRECT:
            _create_directly(user_id, session, gateway, extracted, sources, result=result)
        else:
            _queue(user_id, session, extracted, now=now, result=result)

    result.pending_backlog = pending.count_pending(user_id, session, now=now)
    if result.pending_backlog >= BACKLOG_ALERT_AT:
        message = f"待确认积压 {result.pending_backlog} 条,判据可能太保守了"
        result.warnings.append(message)
        deps.alerter.alert("待确认队列积压", message)


def _create_directly(
    user_id: str,
    session: Session,
    gateway: Gateway,
    extracted,
    sources: Sequence[raw_events.StoredEvent],
    *,
    result: PlanResult,
) -> None:
    ctx = CallContext(
        user_id=user_id,
        agent=AGENT,
        # 如实记来源可信度。L2 允许被外部内容触发(建的东西在自己地盘里、
        # 可回滚),但"这条待办是谁让建的"要查得出来
        trust=trust_of(sources),
        source_event_id=extracted.provenance[0],
        session=session,
    )
    try:
        gateway.call(
            ctx,
            "todo.create",
            {
                "title": extracted.title,
                "starts_at": extracted.starts_at.isoformat() if extracted.starts_at else None,
                "provenance": extracted.provenance,
                "created_by_agent": AGENT,
            },
        )
        result.created += 1
    except Exception as exc:  # noqa: BLE001
        # 一条建失败不该让整个窗口失败:剩下的条目还有价值,而这一条
        # 明天还会被提到(事件还在,待办没建成)
        log.warning("建待办失败,跳过这一条:%s", exc)
        result.warnings.append(f"建待办失败:{exc}")


def _queue(
    user_id: str,
    session: Session,
    extracted,
    *,
    now: datetime,
    result: PlanResult,
) -> None:
    """进待确认队列。

    `payload` 存的是**照它就能写库的形状**,不是给人看的描述 ——
    确认之后由 06 §2.7 那条"同一个事务"的路径原样写进 `todos`。
    """
    pending.enqueue(
        user_id,
        session,
        agent=AGENT,
        kind=(
            pending.PendingKind.CALENDAR_EVENT
            if extracted.kind is ItemKind.SCHEDULE
            else pending.PendingKind.TASK
        ),
        target_table=TARGET_TABLE,
        payload={
            "title": extracted.title,
            "starts_at": extracted.starts_at.isoformat() if extracted.starts_at else None,
            "provenance": extracted.provenance,
            "created_by_agent": AGENT,
        },
        reason=pending.PendingReason(extracted.reason or "low_confidence"),
        confidence=extracted.confidence,
        source_event_id=extracted.provenance[0],
        now=now,
    )
    result.queued += 1
