"""`todos` 仓储的集成测试。需要真实 PostgreSQL(见 conftest.py)。

这一组盯的是**两条 CHECK 和一个两段式的状态**:

- 没有时间的东西不许当成日程写进来 —— 它到了设备上必然写失败,
  而失败发生在手机上,服务端只看得到"一直没同步"
- agent 建的条目必须说得出出处(铁律 5 在待办上的延伸)
- "行写进来了"和"设备确认写进日历了"是两个状态,不是一个
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from lifein.repos.todos import (
    TodoError,
    TodoKind,
    TodoSource,
    TodoStatus,
    create_todo,
    get_todo,
    list_open,
    mark_synced,
    set_status,
    unsynced_schedules,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=6)


def a_todo(session, user_id: str, **overrides):
    params = dict(
        kind=TodoKind.TODO,
        title="帮张三带个充电器",
        source=TodoSource.AGENT,
        provenance=[1],
        created_by_agent="scheduler",
    )
    params.update(overrides)
    return create_todo(user_id, session, **params)


def test_create_and_read_back(pg_session, user_id):
    created = a_todo(pg_session, user_id)

    stored = get_todo(user_id, pg_session, todo_id=created.id)
    assert stored.title == "帮张三带个充电器"
    assert stored.status is TodoStatus.OPEN
    assert stored.provenance == [1]
    assert stored.synced_at is None


def test_schedule_without_a_time_is_rejected(pg_session, user_id):
    with pytest.raises(TodoError):
        a_todo(pg_session, user_id, kind=TodoKind.SCHEDULE, starts_at=None)


def test_database_also_rejects_a_timeless_schedule(pg_session, user_id):
    """绕过仓储直接写,验证 CHECK 本身。

    仓储那道给的是人话,库这道是"仓储被绕过时还有没有防线"。
    """
    with pytest.raises(IntegrityError):
        pg_session.execute(
            text("""
                INSERT INTO todos (user_id, kind, title, source)
                VALUES (:u, 'schedule', '没有时间的日程', 'user')
            """),
            {"u": user_id},
        )


def test_agent_created_item_needs_provenance(pg_session, user_id):
    # 铁律 5 在待办上的延伸:一条"帮张三带个东西"凭空出现在列表里,
    # 比不出现更让人不敢用
    with pytest.raises(TodoError):
        a_todo(pg_session, user_id, provenance=[])


def test_database_also_rejects_agent_items_without_provenance(pg_session, user_id):
    with pytest.raises(IntegrityError):
        pg_session.execute(
            text("""
                INSERT INTO todos (user_id, kind, title, source, provenance)
                VALUES (:u, 'todo', 'agent 凭空建的', 'agent', '{}')
            """),
            {"u": user_id},
        )


def test_user_created_item_needs_no_provenance(pg_session, user_id):
    # 用户自己加的不需要出处 —— 他就是出处
    created = a_todo(
        pg_session, user_id, source=TodoSource.USER, provenance=[], created_by_agent=None
    )
    assert created.provenance == []


def test_sync_is_a_second_state_not_the_same_one(pg_session, user_id):
    """"行写进来了"和"设备确认写进日历了"是两个状态。

    分不开的话,"以为写进日历了其实没有"就查不出来 ——
    而那是这条链路唯一不可接受的失败方式(ADR-020)。
    """
    created = a_todo(pg_session, user_id, kind=TodoKind.SCHEDULE, starts_at=LATER)
    assert created.needs_device_sync is True
    assert [t.id for t in unsynced_schedules(user_id, pg_session)] == [created.id]

    assert mark_synced(user_id, pg_session, todo_id=created.id, device_ref="cal-42") is True

    synced = get_todo(user_id, pg_session, todo_id=created.id)
    assert synced.device_ref == "cal-42"
    assert synced.needs_device_sync is False
    assert unsynced_schedules(user_id, pg_session) == []


def test_empty_device_ref_is_refused(pg_session, user_id):
    # 没有这个 id 就没法回滚设备上那条事件,收下一个空串等于假装同步过了
    created = a_todo(pg_session, user_id, kind=TodoKind.SCHEDULE, starts_at=LATER)
    with pytest.raises(TodoError):
        mark_synced(user_id, pg_session, todo_id=created.id, device_ref="  ")


def test_todos_without_a_time_are_never_queued_for_the_calendar(pg_session, user_id):
    a_todo(pg_session, user_id)
    assert unsynced_schedules(user_id, pg_session) == []


def test_cancel_keeps_the_row(pg_session, user_id):
    """撤销不删行。

    删了的话,审计里那条 L2 调用的 rollback_info 就指向一个不存在的 id ——
    "被回滚过"和"从没发生过"是两回事,后者查不出来。已经同步过的还更糟:
    没人知道设备上还有一条要清理。
    """
    created = a_todo(pg_session, user_id, kind=TodoKind.SCHEDULE, starts_at=LATER)
    mark_synced(user_id, pg_session, todo_id=created.id, device_ref="cal-42")

    assert set_status(user_id, pg_session, todo_id=created.id, status=TodoStatus.CANCELLED)

    stored = get_todo(user_id, pg_session, todo_id=created.id)
    assert stored.status is TodoStatus.CANCELLED
    assert stored.device_ref == "cal-42", "设备上那条还在,id 得留着才删得掉"


def test_setting_the_same_status_twice_reports_no_change(pg_session, user_id):
    # 幂等地返回 False,让调用方能区分"我改的"和"本来就是"
    created = a_todo(pg_session, user_id)
    assert set_status(user_id, pg_session, todo_id=created.id, status=TodoStatus.DONE) is True
    assert set_status(user_id, pg_session, todo_id=created.id, status=TodoStatus.DONE) is False


def test_open_list_puts_timed_items_first(pg_session, user_id):
    """小组件上方寸之地:今天几点要到场的事,比"有空做"的事重要。"""
    a_todo(pg_session, user_id, title="有空做的事")
    a_todo(pg_session, user_id, title="三点开会", kind=TodoKind.SCHEDULE, starts_at=LATER)

    titles = [t.title for t in list_open(user_id, pg_session, until=NOW + timedelta(days=1))]
    assert titles == ["三点开会", "有空做的事"]


def test_open_list_hides_done_and_cancelled(pg_session, user_id):
    keep = a_todo(pg_session, user_id, title="还没做的")
    done = a_todo(pg_session, user_id, title="做完的")
    gone = a_todo(pg_session, user_id, title="撤销的")
    set_status(user_id, pg_session, todo_id=done.id, status=TodoStatus.DONE)
    set_status(user_id, pg_session, todo_id=gone.id, status=TodoStatus.CANCELLED)

    titles = [t.title for t in list_open(user_id, pg_session, until=NOW + timedelta(days=1))]
    assert titles == [keep.title]


def test_far_future_schedules_stay_out_of_the_widget(pg_session, user_id):
    # 小组件问的是"到某个时间点为止有什么",不是"全部未来"
    a_todo(
        pg_session,
        user_id,
        title="下个月的年会",
        kind=TodoKind.SCHEDULE,
        starts_at=NOW + timedelta(days=30),
    )
    assert list_open(user_id, pg_session, until=NOW + timedelta(days=1)) == []


def test_todos_are_isolated_per_user(pg_session, user_id):
    other = str(uuid.uuid4())
    pg_session.execute(
        text("INSERT INTO users (id, display_name, wecom_userid) VALUES (:id, :n, :w)"),
        {"id": other, "n": "另一个人", "w": f"other-{other[:8]}"},
    )
    mine = a_todo(pg_session, user_id)

    assert get_todo(other, pg_session, todo_id=mine.id) is None
    assert list_open(other, pg_session, until=NOW + timedelta(days=1)) == []
    assert set_status(other, pg_session, todo_id=mine.id, status=TodoStatus.DONE) is False
