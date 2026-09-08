"""通知原文保留期的集成测试(R10)。

需要真实 PostgreSQL:被测的东西全是 JSONB 的原地修改,而"改完还是不是一份
合法的归一化事件"只有真库能回答。

四条:到期的清、没到期的不动、**邮件一律不动**、清过的不再清第二遍。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from lifein.jobs.notification_retention import run_once

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def insert(session, user_id, *, source: str, days_ago: int, external_id: str) -> int:
    raw = {"channel": "notification", "title": "项目组", "text": "老王:明天下午三点开会"}
    normalized = {
        "kind": "message",
        "title": "项目组",
        "occurred_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "external_ref": {"source": source, "external_id": external_id},
        "trust": "external",
        "confidence": 1.0,
        "parties": [],
        "body": "老王:明天下午三点开会",
        "flags": [],
    }
    return session.execute(
        text(
            "INSERT INTO raw_events"
            " (user_id, source, external_id, occurred_at, trust, raw, normalized)"
            " VALUES (:u, :s, :e, :t, 'external', CAST(:raw AS JSONB), CAST(:norm AS JSONB))"
            " RETURNING id"
        ),
        {
            "u": user_id,
            "s": source,
            "e": external_id,
            "t": NOW - timedelta(days=days_ago),
            "raw": json.dumps(raw, ensure_ascii=False),
            "norm": json.dumps(normalized, ensure_ascii=False),
        },
    ).scalar_one()


def fetch(session, event_id: int):
    return session.execute(
        text("SELECT raw, normalized FROM raw_events WHERE id = :i"), {"i": event_id}
    ).one()


def test_expired_notification_loses_its_body_but_keeps_the_row(pg_session, user_id):
    """清掉的是"现在还留着",不是"曾经收到过"。"""
    event_id = insert(pg_session, user_id, source="notification", days_ago=30, external_id="old")

    assert run_once(user_id, pg_session, now=NOW, retention_days=7) == 1

    row = fetch(pg_session, event_id)
    assert "text" not in row.raw
    assert row.raw["text_redacted"] is True
    # 元信息留着:群名还在,provenance 指过来时看得到这是哪条
    assert row.raw["title"] == "项目组"
    assert row.normalized["body"] is None
    # 下游要能看出这条是被清过的,不是本来就没正文
    assert "partial" in row.normalized["flags"]


def test_recent_notifications_are_left_alone(pg_session, user_id):
    """提取还要读它 —— 保留期小于补跑窗口的话,补跑会读到空正文。"""
    event_id = insert(pg_session, user_id, source="notification", days_ago=2, external_id="new")

    assert run_once(user_id, pg_session, now=NOW, retention_days=7) == 0
    assert fetch(pg_session, event_id).raw["text"]


def test_emails_are_never_touched(pg_session, user_id):
    """这条规矩管的是别人的话,不是你自己收件箱里的东西。"""
    event_id = insert(pg_session, user_id, source="email", days_ago=90, external_id="mail")

    assert run_once(user_id, pg_session, now=NOW, retention_days=7) == 0
    assert fetch(pg_session, event_id).raw["text"]


def test_running_twice_changes_nothing_more(pg_session, user_id):
    """幂等:清过的不会被再清一遍,flags 里也不会追加第二个 partial。"""
    event_id = insert(pg_session, user_id, source="notification", days_ago=30, external_id="old")

    run_once(user_id, pg_session, now=NOW, retention_days=7)
    assert run_once(user_id, pg_session, now=NOW, retention_days=7) == 0
    assert fetch(pg_session, event_id).normalized["flags"] == ["partial"]


def test_another_users_events_are_out_of_reach(pg_session, user_id):
    """铁律 1:清理也按 user_id 走,不是"把老的都清了"。"""
    import uuid

    other = str(uuid.uuid4())
    pg_session.execute(
        text("INSERT INTO users (id, display_name, wecom_userid) VALUES (:i, :n, :w)"),
        {"i": other, "n": "另一个人", "w": f"o-{other[:8]}"},
    )
    event_id = insert(pg_session, other, source="notification", days_ago=30, external_id="theirs")

    assert run_once(user_id, pg_session, now=NOW, retention_days=7) == 0
    assert fetch(pg_session, event_id).raw["text"]
