"""raw_events 仓储的集成测试。

需要真实 PostgreSQL(见 conftest.py)。没有就整组跳过。

这一组顺带测的是**迁移本身** —— 夹具每次从 base 重建到 head,那几条决定
安全性的 CHECK 约束能不能真的建出来,在这里见分晓。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from lifein.models.normalized import EventKind, ExternalRef, NormalizedEvent, Trust
from lifein.repos.raw_events import count_failed, fetch_normalized_between, insert_events
from lifein.sources.base import IngestedEvent

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


def normalized(external_id: str, *, title: str = "报销单已通过") -> NormalizedEvent:
    return NormalizedEvent(
        kind=EventKind.MESSAGE,
        title=title,
        occurred_at=NOW,
        external_ref=ExternalRef(source="email", external_id=external_id),
        trust=Trust.EXTERNAL,
        confidence=1.0,
        body="正文",
    )


def ingested(external_id: str, **overrides) -> IngestedEvent:
    base = dict(
        source="email",
        external_id=external_id,
        occurred_at=NOW,
        trust=Trust.EXTERNAL,
        raw={"subject": "报销单已通过"},
        normalized=normalized(external_id),
    )
    return IngestedEvent(**{**base, **overrides})


def test_insert_and_read_back(pg_session, user_id):
    result = insert_events(user_id, pg_session, [ingested("m1"), ingested("m2")])
    assert result.inserted == 2

    events = fetch_normalized_between(
        user_id, pg_session, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1)
    )
    assert {e.external_ref.external_id for e in events} == {"m1", "m2"}
    assert events[0].trust is Trust.EXTERNAL


def test_reinsert_is_idempotent(pg_session, user_id):
    """适配器被允许重复返回同一条事件,重复由这里挡掉。"""
    insert_events(user_id, pg_session, [ingested("m1")])
    again = insert_events(user_id, pg_session, [ingested("m1")])

    assert again.inserted == 0
    assert again.duplicates == 1


def test_failed_event_is_stored_with_its_raw(pg_session, user_id):
    # 归一化失败绝不静默丢弃:raw 留着,修好解析器后按同一个键重跑
    result = insert_events(
        user_id,
        pg_session,
        [ingested("m3", normalized=None, normalize_error="缺 Date 头")],
    )
    assert result.inserted == 1 and result.failed == 1

    row = pg_session.execute(
        text("SELECT raw, normalized, normalize_error FROM raw_events WHERE external_id = 'm3'")
    ).one()
    assert row.normalized is None
    assert row.raw["subject"] == "报销单已通过"


def test_failed_events_are_excluded_from_reads(pg_session, user_id):
    insert_events(
        user_id,
        pg_session,
        [ingested("ok"), ingested("bad", normalized=None, normalize_error="x")],
    )
    events = fetch_normalized_between(
        user_id, pg_session, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1)
    )
    assert [e.external_ref.external_id for e in events] == ["ok"]


def test_count_failed_supports_alerting(pg_session, user_id):
    # 数据源改格式时,表现是摘要悄悄变短而不是报错(R8)
    insert_events(
        user_id,
        pg_session,
        [ingested(f"bad{i}", normalized=None, normalize_error="解析失败") for i in range(3)],
    )
    assert count_failed(user_id, pg_session, since=NOW - timedelta(days=1)) == 3


def test_time_window_uses_occurred_at_not_ingested_at(pg_session, user_id):
    """一封昨天的邮件今天才收到,它属于昨天。"""
    yesterday = NOW - timedelta(days=1)
    old = IngestedEvent(
        source="email",
        external_id="old",
        occurred_at=yesterday,
        trust=Trust.EXTERNAL,
        raw={},
        normalized=NormalizedEvent(
            kind=EventKind.MESSAGE,
            title="昨天的邮件",
            occurred_at=yesterday,
            external_ref=ExternalRef(source="email", external_id="old"),
            trust=Trust.EXTERNAL,
            confidence=1.0,
        ),
    )
    insert_events(user_id, pg_session, [old])

    # ingested_at 是刚才,occurred_at 是昨天 —— 今天的窗口里不该有它
    today_only = fetch_normalized_between(
        user_id, pg_session, start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1)
    )
    assert today_only == []


def test_another_user_cannot_see_these_events(pg_session, user_id):
    """铁律 1 的实际效果:换个 user_id 就什么都读不到。"""
    insert_events(user_id, pg_session, [ingested("m1")])
    other = "99999999-9999-9999-9999-999999999999"
    assert (
        fetch_normalized_between(
            other, pg_session, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1)
        )
        == []
    )


class TestSecurityConstraints:
    """那三条 CHECK 是铁律的执行者。这里确认它们真的建出来了。"""

    def test_facts_without_provenance_is_rejected(self, pg_session, user_id):
        # 铁律 5:没有来源的记忆物理上写不进去
        with pytest.raises(IntegrityError):
            pg_session.execute(
                text(
                    "INSERT INTO facts "
                    "(user_id, statement, provenance, confidence, valid_from, created_by_agent) "
                    "VALUES (:u, '他喜欢喝美式', '{}', 0.9, now(), 'memory')"
                ),
                {"u": user_id},
            )

    def test_l3_triggered_by_external_is_rejected(self, pg_session, user_id):
        # 铁律 8:提示注入骗过 agent,也写不进这张表
        with pytest.raises(IntegrityError):
            pg_session.execute(
                text(
                    "INSERT INTO approvals "
                    "(user_id, agent, tool_name, tool_args, preview_text, "
                    " idempotency_key, trigger_trust, expires_at) "
                    "VALUES (:u, 'a', 'send_message', '{}', '给老板发消息', "
                    " 'k1', 'external', now())"
                ),
                {"u": user_id},
            )

    def test_l2_without_rollback_is_rejected(self, pg_session, user_id):
        # 没有回滚信息的 L2 调用写不进审计表,也就等于执行不了
        with pytest.raises(IntegrityError):
            pg_session.execute(
                text(
                    "INSERT INTO tool_calls "
                    "(user_id, agent, tool_name, level, args_digest, result_status) "
                    "VALUES (:u, 'a', 'add_todo', 'L2', '{}', 'allowed')"
                ),
                {"u": user_id},
            )
