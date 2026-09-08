"""记忆抽取 job 的集成测试。需要真实 PostgreSQL(见 conftest.py)。

测的是**记忆真的落到库里了、而且落对了地方**:

- 事实的 `valid_from` 是来源事件的发生时间,不是跑任务的时间
- 用户否定过的东西不会因为任务重跑而回来
- 抽取失败时窗口记 failed,已有的记忆一条不动
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from lifein.alerts import CollectingAlerter
from lifein.jobs.memory_extract import (
    JOB_NAME,
    UNGROUNDED_ALERT_AT,
    MemoryDeps,
    run_once,
)
from lifein.llm.client import LLMClient
from lifein.models.normalized import (
    EventKind,
    ExternalRef,
    IdentifierType,
    NormalizedEvent,
    Party,
    PartyRole,
    Trust,
)
from lifein.repos import facts, job_runs, raw_events
from lifein.repos.entities import AliasType, find_by_alias
from lifein.sources.base import IngestedEvent

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=5)
DAY = timedelta(days=1)


def llm_returning(payload) -> LLMClient:
    text_body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    body = {
        "model": "m",
        "choices": [{"message": {"content": text_body}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    return LLMClient(
        base_url="https://llm.example.com/v1",
        api_key="k",
        model="m",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
        ),
        sleep=lambda _s: None,
    )


def ingested(external_id: str = "m1", *, at: datetime = EARLIER) -> IngestedEvent:
    return IngestedEvent(
        source="email",
        external_id=external_id,
        occurred_at=at,
        trust=Trust.EXTERNAL,
        raw={"subject": "报销单"},
        normalized=NormalizedEvent(
            kind=EventKind.MESSAGE,
            title="报销单",
            occurred_at=at,
            external_ref=ExternalRef(source="email", external_id=external_id),
            trust=Trust.EXTERNAL,
            confidence=1.0,
            body="麻烦你周三前把报销单交给我。",
            parties=[
                Party(
                    role=PartyRole.FROM,
                    display_name="张三",
                    identifier="Zhang@QQ.com",
                    identifier_type=IdentifierType.EMAIL,
                )
            ],
        ),
    )


def seed(session, user_id: str, *events) -> None:
    raw_events.insert_events(user_id, session, list(events) or [ingested()])


def deps(payload, **overrides) -> MemoryDeps:
    return MemoryDeps(
        llm=llm_returning(payload),
        alerter=overrides.pop("alerter", CollectingAlerter()),
        own_identifiers=overrides.pop("own_identifiers", ()),
    )


ONE_FACT = {"facts": [{"statement": "张三在市场部", "refs": ["m1"], "confidence": 0.9}]}


def test_writes_entity_and_fact(pg_session, user_id):
    seed(pg_session, user_id, ingested())

    [result] = run_once(user_id, pg_session, deps=deps(ONE_FACT), now=NOW)

    assert result.entities_created == 1
    assert result.facts_created == 1

    entity = find_by_alias(user_id, pg_session, alias="zhang@qq.com", alias_type=AliasType.EMAIL)
    assert entity is not None
    assert entity.canonical_name == "张三"

    [fact] = facts.list_active_facts(user_id, pg_session, at=NOW)
    assert fact.statement == "张三在市场部"
    assert fact.created_by_agent == "memory"
    # external 来源封顶 0.6 —— 模型给的是 0.9
    assert fact.confidence == pytest.approx(0.6)


def test_valid_from_is_the_events_time_not_now(pg_session, user_id):
    """一封上周的邮件里说的事,是从上周起成立的。

    用 now() 会把所有事实挤在同一天,以后按时间回溯记忆时全乱套。
    """
    seed(pg_session, user_id, ingested(at=EARLIER))
    run_once(user_id, pg_session, deps=deps(ONE_FACT), now=NOW)

    [fact] = facts.list_active_facts(user_id, pg_session, at=NOW)
    assert fact.valid_from == EARLIER


def test_provenance_points_at_raw_events(pg_session, user_id):
    seed(pg_session, user_id, ingested())
    run_once(user_id, pg_session, deps=deps(ONE_FACT), now=NOW)

    [fact] = facts.list_active_facts(user_id, pg_session, at=NOW)
    stored = raw_events.fetch_stored_between(
        user_id, pg_session, start=NOW - DAY, end=NOW + DAY
    )
    assert fact.provenance == [stored[0].event_id], "provenance 必须指得回真实的行"


def test_rerun_does_not_duplicate_or_revive_negated_facts(pg_session, user_id):
    """任务每天跑,那封邮件明天还在。

    否定过的事实要是能被下一次任务写回来,"否定"这个动作就等于没有。
    """
    seed(pg_session, user_id, ingested())
    run_once(user_id, pg_session, deps=deps(ONE_FACT), now=NOW)
    [fact] = facts.list_active_facts(user_id, pg_session, at=NOW)
    facts.negate_fact(user_id, pg_session, fact_id=fact.id)

    # 第二天另一封邮件,模型又抽出同一句话
    seed(pg_session, user_id, ingested("m2", at=NOW + timedelta(hours=1)))
    tomorrow = {"facts": [{"statement": "张三在市场部", "refs": ["m2"], "confidence": 0.9}]}
    [again] = run_once(user_id, pg_session, deps=deps(tomorrow), now=NOW + DAY)

    assert again.facts_created == 0
    assert again.facts_skipped_negated == 1
    assert facts.list_active_facts(user_id, pg_session, at=NOW + DAY) == []


def test_second_sighting_touches_instead_of_creating(pg_session, user_id):
    seed(pg_session, user_id, ingested("m1"))
    run_once(user_id, pg_session, deps=deps(ONE_FACT), now=NOW)

    seed(pg_session, user_id, ingested("m2", at=NOW + timedelta(hours=1)))
    [again] = run_once(user_id, pg_session, deps=deps({"facts": []}), now=NOW + DAY)

    assert again.entities_created == 0
    assert again.entities_touched == 1


def test_own_identifiers_are_not_turned_into_entities(pg_session, user_id):
    mine = ingested()
    mine.normalized.parties.append(
        Party(
            role=PartyRole.TO,
            display_name="我",
            identifier="me@example.com",
            identifier_type=IdentifierType.EMAIL,
        )
    )
    seed(pg_session, user_id, mine)

    run_once(
        user_id,
        pg_session,
        deps=deps({"facts": []}, own_identifiers=["ME@example.com"]),
        now=NOW,
    )

    me = find_by_alias(user_id, pg_session, alias="me@example.com", alias_type=AliasType.EMAIL)
    assert me is None
    assert find_by_alias(user_id, pg_session, alias="zhang@qq.com", alias_type=AliasType.EMAIL)


def test_no_events_is_not_a_failure(pg_session, user_id):
    [result] = run_once(user_id, pg_session, deps=deps(ONE_FACT), now=NOW)

    assert result.no_events is True
    assert result.error is None
    # 安静的一天照样算跑过了,否则下次会当成"从来没跑过"重新补
    assert job_runs.last_successful_window_end(user_id, pg_session, job_name=JOB_NAME) is not None


def test_extraction_failure_leaves_memory_untouched(pg_session, user_id):
    seed(pg_session, user_id, ingested())
    run_once(user_id, pg_session, deps=deps(ONE_FACT), now=NOW)

    seed(pg_session, user_id, ingested("m2", at=NOW + timedelta(hours=1)))
    alerter = CollectingAlerter()
    [result] = run_once(
        user_id,
        pg_session,
        deps=deps("这不是 JSON", alerter=alerter),
        now=NOW + DAY,
    )

    assert result.error
    assert alerter.alerts, "抽取失败要告警:记忆停止更新没有任何外部表现"
    assert len(facts.list_active_facts(user_id, pg_session, at=NOW + DAY)) == 1

    row = pg_session.execute(
        text("""
            SELECT status FROM job_runs
             WHERE user_id = :u AND job_name = :j
             ORDER BY window_start DESC LIMIT 1
        """),
        {"u": user_id, "j": JOB_NAME},
    ).one()
    assert row.status == "failed", "记 failed 这个窗口下次才会被重跑"


def test_ungrounded_facts_raise_an_alert(pg_session, user_id):
    seed(pg_session, user_id, ingested())
    alerter = CollectingAlerter()
    payload = {
        "facts": [
            {"statement": f"编造的第 {i} 条", "refs": [], "confidence": 0.9}
            for i in range(UNGROUNDED_ALERT_AT)
        ]
    }

    [result] = run_once(user_id, pg_session, deps=deps(payload, alerter=alerter), now=NOW)

    assert result.facts_created == 0
    assert result.dropped_ungrounded == UNGROUNDED_ALERT_AT
    assert alerter.alerts


def test_llm_call_is_audited(pg_session, user_id):
    seed(pg_session, user_id, ingested())
    run_once(user_id, pg_session, deps=deps(ONE_FACT), now=NOW)

    row = pg_session.execute(
        text("""
            SELECT agent, tool_name, level, llm_fields_sent
              FROM tool_calls WHERE user_id = :u ORDER BY created_at DESC LIMIT 1
        """),
        {"u": user_id},
    ).one()
    assert row.agent == "memory"
    assert row.tool_name == "llm.chat"
    assert row.level == "L1"
    # 记的是字段名不是内容:要能回答"我到底把什么发给外部供应商了"(R12)
    assert "body" in row.llm_fields_sent


def test_window_is_claimed_once(pg_session, user_id):
    seed(pg_session, user_id, ingested())
    run_once(user_id, pg_session, deps=deps(ONE_FACT), now=NOW)
    results = run_once(user_id, pg_session, deps=deps(ONE_FACT), now=NOW)

    assert results == [] or all(r.skipped or r.no_events for r in results)
