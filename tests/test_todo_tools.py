"""第一批 L2 工具的集成测试。需要真实 PostgreSQL(见 conftest.py)。

**审计走真实的 `PostgresAuditSink`,不是内存假货。** 这一组存在的最大理由是
`tool_calls` 上那条 `l2_needs_rollback` 约束:L2 调用写审计时没有回滚信息,
库会直接拒。用内存 sink 测,那条约束永远不会被碰到 —— 而它正是"L2 必须可回滚"
在数据库层的执行者(06 §2.9)。
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel
from sqlalchemy import text

import lifein.tools.todo as todo_tools
from lifein.agents import contract as agent_contract
from lifein.agents.contract import OnUncertain, agent
from lifein.governance import registry
from lifein.governance.gateway import CallContext, Denied, Gateway
from lifein.models.normalized import Trust
from lifein.repos import todos
from lifein.repos.tool_calls import PostgresAuditSink

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=6)
AGENT = "test_planner"
ALL_TOOLS = ["todo.create", "todo.complete", "todo.cancel"]


class _Args(BaseModel):
    pass


class _Out(BaseModel):
    pass


@pytest.fixture(autouse=True)
def _registered():
    # 别的测试文件会 clear_registry() 做隔离,而模块级的 @tool 只在第一次
    # import 时执行 —— 整套跑起来顺序一变,这里就会拿到空注册表
    if "todo.create" not in registry.registered_tools():
        importlib.reload(todo_tools)
    if AGENT not in agent_contract.registered_agents():

        @agent(
            name=AGENT,
            inputs=_Args,
            tools=ALL_TOOLS,
            output_schema=_Out,
            on_uncertain=OnUncertain.PENDING_CONFIRMATION,
            evalset="evals/test.jsonl",
        )
        def _handler(_: _Args) -> _Out: ...


def gateway_for(user_id: str, session) -> Gateway:
    return Gateway(PostgresAuditSink(user_id, session))


def ctx(session, user_id: str, *, trust: Trust = Trust.USER_INPUT, agent_name=AGENT):
    return CallContext(user_id=user_id, agent=agent_name, trust=trust, session=session)


def audited(session, user_id: str):
    """按 id 排,不按 created_at。

    `created_at DEFAULT now()` 在 PostgreSQL 里是**事务开始时间** ——
    同一个事务里写的几行时间戳完全相同,按它排序拿到的顺序是随意的。
    这个坑在测试里表现为"断言拿到了上一次调用的审计"。
    """
    return session.execute(
        text("""
            SELECT tool_name, level, result_status, rollback_info
              FROM tool_calls WHERE user_id = :u ORDER BY id
        """),
        {"u": user_id},
    ).all()


def create(session, user_id: str, **args):
    payload = {
        "title": "帮张三带个充电器",
        "provenance": [1],
        "created_by_agent": "scheduler",
        **args,
    }
    return gateway_for(user_id, session).call(ctx(session, user_id), "todo.create", payload)


def test_create_todo_writes_a_row_and_records_rollback(pg_session, user_id):
    result = create(pg_session, user_id)

    stored = todos.get_todo(user_id, pg_session, todo_id=result["todo_id"])
    assert stored.title == "帮张三带个充电器"
    assert stored.kind is todos.TodoKind.TODO

    [call] = audited(pg_session, user_id)
    assert call.tool_name == "todo.create"
    assert call.level == "L2"
    # 没有回滚信息的话,库上那条 l2_needs_rollback 会让这行根本写不进来
    assert call.rollback_info == {"cancel_todo_id": stored.id, "kind": "todo"}


def test_a_time_makes_it_a_schedule(pg_session, user_id):
    """给没给时间决定它是待办还是日程。

    分成两个工具也行,但那样 agent 每次都要先判断类型,而"有没有时间"
    本来就是同一个字段。少一次判断就少一类错。
    """
    result = create(pg_session, user_id, starts_at=LATER.isoformat())

    stored = todos.get_todo(user_id, pg_session, todo_id=result["todo_id"])
    assert stored.kind is todos.TodoKind.SCHEDULE
    assert stored.needs_device_sync is True, "写进日历这件事还没发生"


def test_naive_timestamp_is_refused_before_anything_is_written(pg_session, user_id):
    """无时区的时间会在"明天下午三点"上悄悄错八小时。

    而错进日历的日程比没有这个功能糟得多(03 的退出条件)。
    """
    with pytest.raises(Denied):
        create(pg_session, user_id, starts_at="2026-09-08T15:00:00")

    assert todos.list_open(user_id, pg_session, until=NOW + timedelta(days=30)) == []
    # 入参不合法也要留下审计:被拒的调用同样是"发生过的事"
    assert audited(pg_session, user_id)[-1].result_status == "denied"


def test_agent_created_item_without_provenance_is_refused(pg_session, user_id):
    with pytest.raises(Exception):  # noqa: B017 —— 仓储抛 TodoError,经网关原样冒出来
        create(pg_session, user_id, provenance=[])

    assert todos.list_open(user_id, pg_session, until=NOW + timedelta(days=30)) == []
    assert audited(pg_session, user_id)[-1].result_status == "error"


def test_complete_records_the_previous_status_not_a_guess(pg_session, user_id):
    """回滚信息记"之前是什么状态",不是笼统的"改回 open"。

    把一条已经取消的待办"回滚"成待办,是又一次错误的写入。
    """
    created = create(pg_session, user_id)
    gateway_for(user_id, pg_session).call(
        ctx(pg_session, user_id), "todo.complete", {"todo_id": created["todo_id"]}
    )

    stored = todos.get_todo(user_id, pg_session, todo_id=created["todo_id"])
    assert stored.status is todos.TodoStatus.DONE

    call = audited(pg_session, user_id)[-1]
    assert call.rollback_info == {
        "restore_todo_id": created["todo_id"],
        "previous_status": "open",
    }


def test_cancelling_a_synced_schedule_keeps_the_device_ref(pg_session, user_id):
    """撤销一条已经同步的日程,服务端只完成一半。

    另一半在设备上:App 下次同步看到 cancelled 且 device_ref 非空,
    才去把系统日历里那条删掉。所以这行不删,id 也要留在审计里。
    """
    created = create(pg_session, user_id, starts_at=LATER.isoformat())
    todos.mark_synced(user_id, pg_session, todo_id=created["todo_id"], device_ref="cal-42")

    result = gateway_for(user_id, pg_session).call(
        ctx(pg_session, user_id), "todo.cancel", {"todo_id": created["todo_id"]}
    )
    assert result["device_ref"] == "cal-42"

    stored = todos.get_todo(user_id, pg_session, todo_id=created["todo_id"])
    assert stored.status is todos.TodoStatus.CANCELLED
    assert stored.device_ref == "cal-42"
    assert audited(pg_session, user_id)[-1].rollback_info["device_ref"] == "cal-42"


def test_l2_can_be_triggered_by_external_content(pg_session, user_id):
    """L2 允许由外部内容触发,L3 不允许 —— 这是分权的整个意义。

    一封邮件里说"周三开会"就该能建出日程;它能建的东西全在你自己的地盘里,
    而且可回滚。铁律 8 管的是 L3。
    """
    payload = {
        "title": "周三下午三点开会",
        "starts_at": LATER.isoformat(),
        "provenance": [7],
        "created_by_agent": "scheduler",
    }
    result = gateway_for(user_id, pg_session).call(
        ctx(pg_session, user_id, trust=Trust.EXTERNAL), "todo.create", payload
    )
    assert todos.get_todo(user_id, pg_session, todo_id=result["todo_id"]) is not None


def test_agent_without_whitelist_cannot_write(pg_session, user_id):
    if "reader_only" not in agent_contract.registered_agents():

        @agent(
            name="reader_only",
            inputs=_Args,
            tools=[],
            output_schema=_Out,
            on_uncertain=OnUncertain.DO_NOTHING,
            evalset="evals/test.jsonl",
        )
        def _handler(_: _Args) -> _Out: ...

    with pytest.raises(Denied):
        gateway_for(user_id, pg_session).call(
            ctx(pg_session, user_id, agent_name="reader_only"),
            "todo.create",
            {"title": "不该建出来的东西", "provenance": [1], "created_by_agent": "x"},
        )
    assert todos.list_open(user_id, pg_session, until=NOW + timedelta(days=30)) == []
