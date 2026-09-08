"""待确认队列的集成测试。需要真实 PostgreSQL(见 conftest.py)。

这一组盯的是 06 §2.7 那三条约束,每一条都对应一种"看起来正常、实际很难查"的错:

- 确认与写入不在同一个事务 → "确认了但没写进去"
- 认领不排他 → 两个入口同时点,写了两遍
- rejected / expired 被删掉 → agent 每次都从头再错一遍
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from lifein.repos.pending import (
    DEFAULT_TTL,
    PendingError,
    PendingKind,
    PendingReason,
    PendingStatus,
    confirm,
    count_pending,
    enqueue,
    expire_overdue,
    get,
    list_pending,
    reject,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)


def queue_one(session, user_id: str, **overrides):
    params = dict(
        agent="planner",
        kind=PendingKind.CALENDAR_EVENT,
        target_table="todos",
        payload={"title": "周三下午三点开会", "starts_at": "2026-09-09T15:00:00+08:00"},
        reason=PendingReason.LOW_CONFIDENCE,
        confidence=0.5,
        now=NOW,
    )
    params.update(overrides)
    return enqueue(user_id, session, **params)


def test_enqueue_and_list(pg_session, user_id):
    created = queue_one(pg_session, user_id)

    assert created.status is PendingStatus.PENDING
    assert created.expires_at == NOW + DEFAULT_TTL
    assert [p.id for p in list_pending(user_id, pg_session, now=NOW)] == [created.id]
    assert count_pending(user_id, pg_session, now=NOW) == 1


def test_payload_must_be_writable_not_prose(pg_session, user_id):
    # payload 存的是 agent 原本想写入的内容,确认之后照它写库。
    # 空 payload 意味着确认之后无事可做,那这条根本不该进队列
    with pytest.raises(PendingError):
        queue_one(pg_session, user_id, payload={})


def test_confirm_writes_in_the_same_transaction(pg_session, user_id):
    """确认与写入同一个事务 —— 这是 06 §2.7 的硬要求。"""
    created = queue_one(pg_session, user_id)
    written: list[dict] = []

    result = confirm(
        user_id,
        pg_session,
        pending_id=created.id,
        writer=lambda payload: written.append(payload) or "todo-1",
        resolved_via="app",
    )

    assert result == "todo-1", "writer 的返回值原样透传,调用方要那个新建行的 id"
    assert written[0]["title"] == "周三下午三点开会"
    assert get(user_id, pg_session, pending_id=created.id).status is PendingStatus.CONFIRMED


def test_writer_failure_rolls_back_the_status_too(pg_session, user_id):
    """写入炸了,状态也不许留在 confirmed。

    "确认了但没写进去"是这里唯一不能出现的状态:用户以为处理完了,
    而那件事根本没发生,再也不会有人提起它。
    """
    created = queue_one(pg_session, user_id)

    def boom(_payload):
        raise RuntimeError("目标表写失败")

    # 入队发生在更早的事务里,确认是这一次的工作单元 —— 用 SAVEPOINT 把
    # "这一次"圈出来,回滚它不该把入队也一起抹掉
    with pytest.raises(RuntimeError), pg_session.begin_nested():
        confirm(user_id, pg_session, pending_id=created.id, writer=boom, resolved_via="app")

    assert get(user_id, pg_session, pending_id=created.id).status is PendingStatus.PENDING


def test_second_confirm_gets_nothing_instead_of_writing_twice(pg_session, user_id):
    """两个入口同时点确认是正常的用户行为,不是错误。

    所以第二次拿到 None(已经处理过了),而不是把同一件事写两遍。
    """
    created = queue_one(pg_session, user_id)
    calls: list[dict] = []

    first = confirm(
        user_id,
        pg_session,
        pending_id=created.id,
        writer=lambda p: calls.append(p) or "todo-1",
        resolved_via="app",
    )
    second = confirm(
        user_id,
        pg_session,
        pending_id=created.id,
        writer=lambda p: calls.append(p) or "todo-2",
        resolved_via="weixin",
    )

    assert first == "todo-1"
    assert second is None
    assert len(calls) == 1


def test_edited_payload_is_what_gets_written(pg_session, user_id):
    created = queue_one(pg_session, user_id)
    written: list[dict] = []

    confirm(
        user_id,
        pg_session,
        pending_id=created.id,
        writer=lambda p: written.append(p),
        resolved_via="app",
        edited_payload={"title": "周三下午四点开会", "starts_at": "2026-09-09T16:00:00+08:00"},
    )

    assert written[0]["title"] == "周三下午四点开会"
    stored = get(user_id, pg_session, pending_id=created.id)
    assert stored.status is PendingStatus.EDITED, "改过再确认要和原样确认分得开"


def test_rejected_record_is_kept(pg_session, user_id):
    """用户拒绝过什么,正是这个 agent 最该学会不做的事。

    删掉等于每次都从头再错一遍 —— 那是评测集的负样本来源。
    """
    created = queue_one(pg_session, user_id)
    assert reject(user_id, pg_session, pending_id=created.id, resolved_via="app") is True

    stored = get(user_id, pg_session, pending_id=created.id)
    assert stored.status is PendingStatus.REJECTED
    assert list_pending(user_id, pg_session, now=NOW) == []


def test_rejected_cannot_be_confirmed_afterwards(pg_session, user_id):
    created = queue_one(pg_session, user_id)
    reject(user_id, pg_session, pending_id=created.id, resolved_via="app")

    assert (
        confirm(
            user_id, pg_session, pending_id=created.id, writer=lambda p: "x", resolved_via="app"
        )
        is None
    )


def test_expired_items_drop_out_and_cannot_be_confirmed(pg_session, user_id):
    """一条三十天前的"周三开会",现在确认了反而更糟。"""
    created = queue_one(pg_session, user_id)
    later = NOW + DEFAULT_TTL + timedelta(days=1)

    assert list_pending(user_id, pg_session, now=later) == []
    assert expire_overdue(user_id, pg_session, now=later) == 1
    assert get(user_id, pg_session, pending_id=created.id).status is PendingStatus.EXPIRED

    assert (
        confirm(
            user_id, pg_session, pending_id=created.id, writer=lambda p: "x", resolved_via="app"
        )
        is None
    )


def test_expiring_twice_is_a_no_op(pg_session, user_id):
    queue_one(pg_session, user_id)
    later = NOW + DEFAULT_TTL + timedelta(days=1)
    assert expire_overdue(user_id, pg_session, now=later) == 1
    assert expire_overdue(user_id, pg_session, now=later) == 0


def test_source_event_id_points_back_at_the_event(pg_session, user_id):
    """溯源:这条待确认是从哪封邮件来的。

    没有它,用户在列表里看到一条"帮张三带个东西"只能凭空判断对不对。
    """
    event_id = pg_session.execute(
        text("""
            INSERT INTO raw_events (user_id, source, external_id, occurred_at, trust, raw)
            VALUES (:u, 'email', 'm1', :t, 'external', '{}') RETURNING id
        """),
        {"u": user_id, "t": NOW},
    ).scalar_one()

    created = queue_one(pg_session, user_id, source_event_id=event_id)
    assert get(user_id, pg_session, pending_id=created.id).source_event_id == event_id


def test_queue_is_isolated_per_user(pg_session, user_id):
    other = str(uuid.uuid4())
    pg_session.execute(
        text("INSERT INTO users (id, display_name, wecom_userid) VALUES (:id, :n, :w)"),
        {"id": other, "n": "另一个人", "w": f"other-{other[:8]}"},
    )
    mine = queue_one(pg_session, user_id)

    assert list_pending(other, pg_session, now=NOW) == []
    assert get(other, pg_session, pending_id=mine.id) is None
    assert reject(other, pg_session, pending_id=mine.id, resolved_via="app") is False
