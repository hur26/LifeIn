"""日程与待办提取 job 的集成测试。需要真实 PostgreSQL(见 conftest.py)。

**这是第一条会写东西的自动链路**,所以这一组测的是"写了什么、没写什么":

- 日程进队列,`todos` 里一条都不该有
- 够有把握的待办直接建,而且那次写入留在了 `tool_calls` 里(带回滚信息)
- 建失败一条不影响其余
- 队列积压会告警 —— 它和"误报进日历"是同一枚硬币的两面
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from lifein.alerts import CollectingAlerter
from lifein.jobs.plan_extract import (
    BACKLOG_ALERT_AT,
    JOB_NAME,
    PlanDeps,
    run_once,
)
from lifein.llm.client import LLMClient
from lifein.models.normalized import EventKind, ExternalRef, NormalizedEvent, Trust
from lifein.repos import job_runs, pending, todos
from lifein.repos.raw_events import insert_events
from lifein.sources.base import IngestedEvent

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=3)
FUTURE = (NOW + timedelta(days=1)).isoformat()
DAY = timedelta(days=1)


@pytest.fixture(autouse=True)
def _registered():
    # 别的测试文件会 clear_registry(),而模块级的 @tool / @agent 只在第一次
    # import 时执行 —— 顺序一变这里就会拿到空注册表
    import importlib

    import lifein.agents.planner as planner_agent
    import lifein.tools.todo as todo_tools
    from lifein.agents import contract
    from lifein.governance import registry

    if "todo.create" not in registry.registered_tools():
        importlib.reload(todo_tools)
    if "planner" not in contract.registered_agents():
        importlib.reload(planner_agent)


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
        max_retries=0,
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
        ),
        sleep=lambda _s: None,
    )


def seed(session, user_id: str, external_id: str = "m1", *, at: datetime = EARLIER) -> None:
    insert_events(
        user_id,
        session,
        [
            IngestedEvent(
                source="email",
                external_id=external_id,
                occurred_at=at,
                trust=Trust.EXTERNAL,
                raw={},
                normalized=NormalizedEvent(
                    kind=EventKind.MESSAGE,
                    title="项目周会",
                    occurred_at=at,
                    external_ref=ExternalRef(source="email", external_id=external_id),
                    trust=Trust.EXTERNAL,
                    confidence=1.0,
                    body="周三下午三点开个会,顺便帮我带个充电器。",
                ),
            )
        ],
    )


def deps(items, alerter=None) -> PlanDeps:
    return PlanDeps(
        llm=llm_returning({"items": items}), alerter=alerter or CollectingAlerter()
    )


TODO_ITEM = {"kind": "todo", "title": "帮张三带个充电器", "refs": ["m1"], "confidence": 0.9}
SCHEDULE_ITEM = {
    "kind": "schedule",
    "title": "项目周会",
    "starts_at": FUTURE,
    "refs": ["m1"],
    "confidence": 0.99,
}


def open_todos(session, user_id: str):
    return todos.list_open(user_id, session, until=NOW + timedelta(days=30))


def test_confident_todo_is_created_and_audited(pg_session, user_id):
    seed(pg_session, user_id)

    [result] = run_once(user_id, pg_session, deps=deps([TODO_ITEM]), now=NOW)

    assert result.created == 1
    assert result.queued == 0
    [created] = open_todos(pg_session, user_id)
    assert created.title == "帮张三带个充电器"
    assert created.created_by_agent == "planner"

    row = pg_session.execute(
        text("""
            SELECT tool_name, level, rollback_info FROM tool_calls
             WHERE user_id = :u AND tool_name = 'todo.create' ORDER BY id DESC LIMIT 1
        """),
        {"u": user_id},
    ).one()
    # 这条带 rollback_info 的记录,是"这条待办哪来的、怎么撤"唯一的答案
    assert row.level == "L2"
    assert row.rollback_info["cancel_todo_id"] == created.id


def test_schedules_never_reach_the_calendar_directly(pg_session, user_id):
    """日程一律进队列。03 的验收标准是"误报进日历的条数为 0"。"""
    seed(pg_session, user_id)

    [result] = run_once(user_id, pg_session, deps=deps([SCHEDULE_ITEM]), now=NOW)

    assert result.created == 0
    assert result.queued == 1
    assert open_todos(pg_session, user_id) == [], "确认之前 todos 里一条都不该有"

    [queued] = pending.list_pending(user_id, pg_session, now=NOW)
    assert queued.kind is pending.PendingKind.CALENDAR_EVENT
    assert queued.target_table == "todos"
    assert queued.source_event_id is not None, "队列里的条目要指得回那封邮件"


def test_confirming_a_queued_schedule_writes_it_through(pg_session, user_id):
    """确认之后照 payload 写库 —— payload 存的就是能直接落库的形状。"""
    seed(pg_session, user_id)
    run_once(user_id, pg_session, deps=deps([SCHEDULE_ITEM]), now=NOW)
    [queued] = pending.list_pending(user_id, pg_session, now=NOW)

    def write(payload):
        return todos.create_todo(
            user_id,
            pg_session,
            kind=todos.TodoKind.SCHEDULE,
            title=payload["title"],
            source=todos.TodoSource.AGENT,
            starts_at=datetime.fromisoformat(payload["starts_at"]),
            provenance=payload["provenance"],
            created_by_agent=payload["created_by_agent"],
        ).id

    todo_id = pending.confirm(
        user_id, pg_session, pending_id=queued.id, writer=write, resolved_via="app"
    )

    stored = todos.get_todo(user_id, pg_session, todo_id=todo_id)
    assert stored.title == "项目周会"
    assert stored.needs_device_sync is True, "写进系统日历这件事还没发生"


def test_low_confidence_todo_goes_to_the_queue(pg_session, user_id):
    seed(pg_session, user_id)
    weak = {**TODO_ITEM, "confidence": 0.3}

    [result] = run_once(user_id, pg_session, deps=deps([weak]), now=NOW)

    assert result.queued == 1
    assert open_todos(pg_session, user_id) == []
    [queued] = pending.list_pending(user_id, pg_session, now=NOW)
    assert queued.reason is pending.PendingReason.LOW_CONFIDENCE


def test_past_and_ungrounded_items_are_counted_not_written(pg_session, user_id):
    seed(pg_session, user_id)
    items = [
        {**SCHEDULE_ITEM, "starts_at": (NOW - timedelta(days=2)).isoformat()},
        {**TODO_ITEM, "refs": []},
    ]

    [result] = run_once(user_id, pg_session, deps=deps(items), now=NOW)

    assert result.dropped_past == 1
    assert result.dropped_ungrounded == 1
    assert result.created == 0
    assert result.queued == 0
    assert pending.list_pending(user_id, pg_session, now=NOW) == []


def test_one_failing_item_does_not_stop_the_rest(pg_session, user_id):
    """一条建失败不该让整个窗口失败。

    剩下的条目还有价值,而失败那条明天还会被提到 —— 事件还在,待办没建成。
    """
    seed(pg_session, user_id)
    items = [
        {**TODO_ITEM, "title": "会" * 200},  # 超长,工具入参校验会拒
        {**TODO_ITEM, "title": "帮张三带个充电器"},
    ]

    [result] = run_once(user_id, pg_session, deps=deps(items), now=NOW)

    assert result.error is None
    assert [t.title for t in open_todos(pg_session, user_id)] == ["帮张三带个充电器"]


def test_expired_items_are_swept_before_extracting(pg_session, user_id):
    seed(pg_session, user_id)
    run_once(user_id, pg_session, deps=deps([SCHEDULE_ITEM]), now=NOW)

    seed(pg_session, user_id, "m2", at=NOW + timedelta(days=40))
    much_later = NOW + pending.DEFAULT_TTL + timedelta(days=1)
    # 隔了一个月再跑,会补几个窗口(job_runs 的补偿上限),过期清理只在
    # 第一个窗口里真的清到东西
    results = run_once(user_id, pg_session, deps=deps([]), now=much_later)

    assert sum(r.expired for r in results) == 1
    assert pending.list_pending(user_id, pg_session, now=much_later) == []


def test_backlog_raises_an_alert(pg_session, user_id):
    """积压不是"处理不完",是判据太保守的信号。

    一个每天堆三条待确认的系统,和一个每天误报三条的系统,最后都会被关掉。
    """
    seed(pg_session, user_id)
    alerter = CollectingAlerter()
    items = [
        {**SCHEDULE_ITEM, "title": f"会议 {i}"} for i in range(BACKLOG_ALERT_AT)
    ]

    [result] = run_once(user_id, pg_session, deps=deps(items, alerter), now=NOW)

    assert result.queued == BACKLOG_ALERT_AT
    assert result.pending_backlog >= BACKLOG_ALERT_AT
    assert alerter.alerts


def test_no_events_is_not_a_failure(pg_session, user_id):
    [result] = run_once(user_id, pg_session, deps=deps([TODO_ITEM]), now=NOW)

    assert result.no_events is True
    assert result.error is None
    assert job_runs.last_successful_window_end(user_id, pg_session, job_name=JOB_NAME) is not None


def test_extraction_failure_writes_nothing(pg_session, user_id):
    seed(pg_session, user_id)
    alerter = CollectingAlerter()

    [result] = run_once(
        user_id,
        pg_session,
        deps=PlanDeps(llm=llm_returning("这不是 JSON"), alerter=alerter),
        now=NOW,
    )

    assert result.error
    assert alerter.alerts
    assert open_todos(pg_session, user_id) == []
    assert pending.list_pending(user_id, pg_session, now=NOW) == []
