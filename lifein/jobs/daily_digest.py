"""每日摘要 job —— 架构 §5 那条 P0 完整链路。

    邮箱 / 日历 → 归一化 → raw_events → 构造 prompt → 外部 LLM
                                                    ↓
                       push_log + tool_calls ← 企微推送 ← 渲染

这个模块本身几乎不含逻辑,它的价值在于**把失败关在正确的地方**:

- **一个数据源挂了,不影响另一个。** 邮箱认证失效不该让日历也采不到,
  更不该让今天没有摘要 —— 少几条素材的摘要仍然有用。
- **归一化失败要告警但不阻断。** 数据源改格式时表现是摘要悄悄变短(R8),
  没有这条告警就只能靠"你哪天觉得不对劲"发现。
- **摘要生成失败就整条不发,并记 failed。** 发一条错的比不发更伤信任(R4);
  记 failed 意味着这个窗口下次还会被算进来重跑。
- **推送失败也要写 push_log。** 频率闸门只数送达的,但"发过但没送到"是排查
  企微问题时唯一的线索。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from lifein.agents.digest import DigestFailed, DigestInput, run_digest, to_card
from lifein.alerts import Alerter
from lifein.channels.base import Channel
from lifein.governance.audit import ToolCallRecord
from lifein.governance.registry import ToolLevel
from lifein.llm.client import LLMClient
from lifein.repos import job_runs, push_log, raw_events
from lifein.repos.tool_calls import record_tool_call
from lifein.sources.base import PullAdapter

log = logging.getLogger(__name__)

JOB_NAME = "daily_digest"


@dataclass(frozen=True)
class DigestDeps:
    adapters: Sequence[PullAdapter]
    llm: LLMClient
    channel: Channel
    alerter: Alerter


@dataclass
class WindowResult:
    window_start: datetime
    window_end: datetime
    skipped: bool = False
    """窗口已被认领过。补跑时正常出现,不是错误。"""

    inserted: int = 0
    duplicates: int = 0
    normalize_failed: int = 0
    events_considered: int = 0
    pushed: bool = False
    error: str | None = None
    failed_sources: list[str] = field(default_factory=list)

    def as_stats(self) -> dict:
        return {
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "normalize_failed": self.normalize_failed,
            "events_considered": self.events_considered,
            "pushed": self.pushed,
            "failed_sources": self.failed_sources,
        }


def run_once(
    user_id: str,
    session: Session,
    *,
    deps: DigestDeps,
    now: datetime,
    window: timedelta = timedelta(days=1),
) -> list[WindowResult]:
    """跑该跑的窗口(通常一个,补跑时多个)。"""
    windows = job_runs.windows_to_run(user_id, session, job_name=JOB_NAME, now=now, length=window)
    return [
        _run_window(user_id, session, deps=deps, start=start, end=end) for start, end in windows
    ]


def _run_window(
    user_id: str,
    session: Session,
    *,
    deps: DigestDeps,
    start: datetime,
    end: datetime,
) -> WindowResult:
    result = WindowResult(window_start=start, window_end=end)

    if not job_runs.claim_window(
        user_id, session, job_name=JOB_NAME, window_start=start, window_end=end
    ):
        result.skipped = True
        return result

    try:
        _ingest(user_id, session, deps=deps, since=start, result=result)
        _summarize_and_push(user_id, session, deps=deps, start=start, end=end, result=result)
    except DigestFailed as exc:
        result.error = str(exc)
        deps.alerter.alert("每日摘要没生成出来", str(exc))
    except Exception as exc:  # noqa: BLE001
        # 没预料到的异常也要落成 failed,否则这个窗口会被当成"成功过"再也不补
        log.exception("摘要 job 异常")
        result.error = f"{type(exc).__name__}: {exc}"
        deps.alerter.alert("每日摘要异常终止", result.error)

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


def _ingest(
    user_id: str,
    session: Session,
    *,
    deps: DigestDeps,
    since: datetime,
    result: WindowResult,
) -> None:
    for adapter in deps.adapters:
        try:
            events = list(adapter.fetch(since))
        except Exception as exc:  # noqa: BLE001
            # 一个源挂了不影响另一个,也不该让今天没有摘要 ——
            # 少几条素材的摘要仍然有用
            log.exception("数据源 %s 采集失败", adapter.source)
            result.failed_sources.append(adapter.source)
            deps.alerter.alert(f"数据源 {adapter.source} 采集失败", f"{type(exc).__name__}: {exc}")
            continue

        inserted = raw_events.insert_events(user_id, session, events)
        result.inserted += inserted.inserted
        result.duplicates += inserted.duplicates
        result.normalize_failed += inserted.failed

    if result.normalize_failed:
        # 数据源改格式的表现是摘要悄悄变短,不是报错(R8)
        deps.alerter.alert(
            "有事件归一化失败",
            f"{result.normalize_failed} 条已入库但没解析出来,查 raw_events.normalize_error",
        )


def _summarize_and_push(
    user_id: str,
    session: Session,
    *,
    deps: DigestDeps,
    start: datetime,
    end: datetime,
    result: WindowResult,
) -> None:
    events = raw_events.fetch_normalized_between(user_id, session, start=start, end=end)
    digest = run_digest(DigestInput(day=end.date(), events=events), llm=deps.llm)
    result.events_considered = digest.output.considered_events

    # LLM 调用记进审计:它是这条链路上唯一花钱、也唯一把数据发到外部的一步。
    # level 记 L1 —— 生成摘要不写任何外部世界的东西。
    record_tool_call(
        user_id,
        session,
        ToolCallRecord(
            user_id=user_id,
            agent="daily_digest",
            tool_name="llm.chat",
            level=ToolLevel.L1,
            args_digest={"events": {"type": "list", "len": len(events)}},
            llm_fields_sent=digest.llm_fields_sent,
            result_status="allowed",
            prompt_tokens=digest.prompt_tokens,
            completion_tokens=digest.completion_tokens,
        ),
    )

    card = to_card(digest.output, end.date())
    try:
        delivery = deps.channel.send(user_id, card)
    except Exception as exc:  # noqa: BLE001
        # 推送失败也要写 push_log:"发过但没送到"是排查企微问题时唯一的线索
        push_log.record_push(
            user_id,
            session,
            channel=deps.channel.name,
            mode="active",
            card=card,
            delivered=False,
            error=f"{type(exc).__name__}: {exc}",
        )
        deps.alerter.alert("摘要推送失败", str(exc))
        result.error = f"推送失败:{exc}"
        return

    push_log.record_push(
        user_id,
        session,
        channel=delivery.channel,
        mode="active",
        card=card,
        delivered=True,
    )
    result.pushed = True
