"""记忆抽取 job —— 每天把事件流里的东西沉进实体库和事实库。

    raw_events → memory agent → entities / entity_aliases / facts
                     ↓
                 tool_calls(记这次调模型花了多少、发了哪些字段)

**它不采集。** 采集是摘要 job 的事,这里只读已经落库的 `raw_events` ——
两个 job 各自认领自己的窗口,谁也不等谁。这样摘要链路一个字节不用改
(P0 正在验收期里跑),而记忆抽取挂掉也不会连累每天的摘要。

**写库的规则全在仓储里,这里一条都不重复。** external 封顶 0.6、被否定的
事实不再写回、别名冲突不动作 —— 都在 `repos/facts.py` 和 `repos/entities.py`。
这个模块只负责把 agent 的输出逐条递过去,并把发生了什么数出来。

数出来的那些数字不是装饰:P1 的退出条件之一是"记忆里开始出现你不认可又说
不清来源的条目",而 `dropped_ungrounded` 与 `conflicts` 就是它的早期信号。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from lifein.agents.memory import MemoryFailed, MemoryInput, extract
from lifein.alerts import Alerter
from lifein.governance.audit import ToolCallRecord
from lifein.governance.registry import ToolLevel
from lifein.llm.client import LLMClient, LLMError
from lifein.repos import embeddings, entities, facts, job_runs, raw_events, users
from lifein.repos.tool_calls import record_tool_call

log = logging.getLogger(__name__)

JOB_NAME = "memory_extract"

EMBED_BATCH = 50
"""一次最多给多少条事实补向量。

补不完不要紧,明天接着补 —— `facts_missing_embeddings` 问的是"谁还没有",
不是"这次抽了谁",所以漏掉的会一直排在队首。
"""

UNGROUNDED_ALERT_AT = 5
"""一个窗口里丢掉多少条无来源事实就告警。

选 5 不是算出来的:偶尔一两条是模型的正常抖动,连着五条说明 prompt 或者
素材出了系统性问题 —— 那时候记忆正在悄悄停止更新,而它没有任何外部表现。
"""


@dataclass(frozen=True)
class MemoryDeps:
    llm: LLMClient
    alerter: Alerter
    own_identifiers: Sequence[str] = ()
    """用户本人的邮箱等标识符。企微 userid 由 job 自己从 `users` 里补上。"""


@dataclass
class ExtractResult:
    window_start: datetime
    window_end: datetime
    skipped: bool = False
    no_events: bool = False

    events_considered: int = 0
    entities_created: int = 0
    entities_touched: int = 0
    conflicts: int = 0
    """别名指向了另一个实体。**不是错误**,是"这里可能有两个同名的人"的记号。"""

    facts_created: int = 0
    embedded: int = 0
    """这次补了多少条向量。没配 EMBEDDING_MODEL 时恒为 0(ADR-019)。"""

    facts_merged: int = 0
    facts_skipped_negated: int = 0
    """用户否定过、这次没写回去的条数。这个数字越大说明抽取越该改。"""

    dropped_ungrounded: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_stats(self) -> dict:
        return {
            "events_considered": self.events_considered,
            "entities_created": self.entities_created,
            "entities_touched": self.entities_touched,
            "conflicts": self.conflicts,
            "facts_created": self.facts_created,
            "embedded": self.embedded,
            "facts_merged": self.facts_merged,
            "facts_skipped_negated": self.facts_skipped_negated,
            "dropped_ungrounded": self.dropped_ungrounded,
            "no_events": self.no_events,
        }


def run_once(
    user_id: str,
    session: Session,
    *,
    deps: MemoryDeps,
    now: datetime,
    window: timedelta = timedelta(days=1),
) -> list[ExtractResult]:
    windows = job_runs.windows_to_run(user_id, session, job_name=JOB_NAME, now=now, length=window)
    return [
        _run_window(user_id, session, deps=deps, start=start, end=end, now=now)
        for start, end in windows
    ]


def _run_window(
    user_id: str,
    session: Session,
    *,
    deps: MemoryDeps,
    start: datetime,
    end: datetime,
    now: datetime,
) -> ExtractResult:
    result = ExtractResult(window_start=start, window_end=end)

    if not job_runs.claim_window(
        user_id, session, job_name=JOB_NAME, window_start=start, window_end=end, now=now
    ):
        result.skipped = True
        return result

    try:
        _extract_window(user_id, session, deps=deps, start=start, end=end, result=result)
    except MemoryFailed as exc:
        # 抽取失败不影响任何已有数据:这个窗口记 failed,下次还会被算进来重跑
        result.error = str(exc)
        deps.alerter.alert("记忆抽取失败", str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("记忆抽取 job 异常")
        result.error = f"{type(exc).__name__}: {exc}"
        deps.alerter.alert("记忆抽取异常终止", result.error)

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
    deps: MemoryDeps,
    start: datetime,
    end: datetime,
    result: ExtractResult,
) -> None:
    stored = raw_events.fetch_stored_between(user_id, session, start=start, end=end)
    if not stored:
        # 安静的一天。不告警 —— 噪音会让真出事的那次被忽略(R4)
        log.info("窗口 %s 内没有事件,记忆不更新", start.isoformat())
        result.no_events = True
        return

    user = users.get_user(user_id, session)
    own = [*deps.own_identifiers]
    if user:
        # 企微 userid 也是"自己":日程的组织者就是你本人
        own.append(user.wecom_userid)

    memory = extract(MemoryInput(events=stored, own_identifiers=own), llm=deps.llm)
    output = memory.output
    result.events_considered = output.considered_events
    result.dropped_ungrounded = output.dropped_ungrounded

    record_tool_call(
        user_id,
        session,
        ToolCallRecord(
            user_id=user_id,
            agent="memory",
            tool_name="llm.chat",
            level=ToolLevel.L1,  # 只读事件、只写自己的记忆表,不碰外部世界
            args_digest={"events": {"type": "list", "len": len(stored)}},
            llm_fields_sent=memory.llm_fields_sent,
            result_status="allowed",
            prompt_tokens=memory.prompt_tokens,
            completion_tokens=memory.completion_tokens,
        ),
    )

    _write_sightings(user_id, session, output.sightings, result=result)
    _write_facts(user_id, session, output.facts, stored=stored, result=result)
    _embed_pending_facts(user_id, session, deps=deps, result=result)

    if result.dropped_ungrounded >= UNGROUNDED_ALERT_AT:
        # 记忆停止更新是没有外部表现的:摘要照发,只是它慢慢不再"记得你"
        message = f"本次有 {result.dropped_ungrounded} 条事实指不回来源,已全部丢弃"
        result.warnings.append(message)
        deps.alerter.alert("记忆抽取的溯源率异常", message)


def _embed_pending_facts(
    user_id: str,
    session: Session,
    *,
    deps: MemoryDeps,
    result: ExtractResult,
) -> None:
    """给还没有向量的事实补上向量。

    **失败不影响这次抽取。** 事实已经写进库了,少了向量只是这几条暂时召不回来,
    而明天这个任务还会挑到它们。为此把整个窗口记成 failed 会让已经写好的
    记忆被当成没写过重跑一遍 —— 那才是真的损失。
    """
    if not deps.llm.embeddings_enabled:
        return

    model = deps.llm.embedding_model
    pending = embeddings.facts_missing_embeddings(
        user_id, session, model=model, limit=EMBED_BATCH
    )
    if not pending:
        return

    try:
        vectors = deps.llm.embed([item.statement for item in pending])
    except LLMError as exc:
        log.warning("补向量失败,这批留到下次:%s", exc)
        result.warnings.append(f"向量没算成:{exc}")
        return

    for item, vector in zip(pending, vectors, strict=True):
        embeddings.upsert(
            user_id,
            session,
            ref_type=embeddings.RefType.FACT,
            ref_id=item.fact_id,
            embedding=vector,
            model=model,
        )
    result.embedded = len(pending)


def _write_sightings(
    user_id: str,
    session: Session,
    sightings,
    *,
    result: ExtractResult,
) -> None:
    for sighting in sightings:
        resolution = entities.resolve_or_create(
            user_id,
            session,
            kind=sighting.kind,
            name=sighting.name,
            seen_at=sighting.seen_at,
            identifier=sighting.identifier,
            identifier_type=sighting.identifier_type,
            evidence_event_id=sighting.event_id,
        )
        if resolution.created:
            result.entities_created += 1
        else:
            result.entities_touched += 1
        if resolution.conflicted:
            # 同名不同标识符。仓储那边一行没改,这里只把它数出来
            result.conflicts += 1


def _write_facts(
    user_id: str,
    session: Session,
    extracted,
    *,
    stored: Sequence[raw_events.StoredEvent],
    result: ExtractResult,
) -> None:
    occurred = {item.event_id: item.event.occurred_at for item in stored}

    for fact in extracted:
        # valid_from 取最早那条来源事件的发生时间,不是"现在":
        # 一封上周的邮件里说的事,是从上周起成立的。用 now() 会让所有事实
        # 挤在同一天,以后按时间回溯记忆时全乱套
        valid_from = min(
            (occurred[event_id] for event_id in fact.provenance if event_id in occurred),
            default=None,
        )
        if valid_from is None:
            # provenance 里的 id 不在本窗口 —— agent 只从本窗口的素材建索引,
            # 走到这里说明它编了一个 id,不写
            log.warning("事实的来源不在本窗口内,跳过:%s", fact.statement[:40])
            result.dropped_ungrounded += 1
            continue

        outcome = facts.add_fact(
            user_id,
            session,
            statement=fact.statement,
            provenance=fact.provenance,
            confidence=fact.confidence,
            trust=fact.trust,
            created_by_agent="memory",
            valid_from=valid_from,
        )
        if outcome.created:
            result.facts_created += 1
        elif outcome.reason == "negated":
            result.facts_skipped_negated += 1
        else:
            result.facts_merged += 1
