"""分权网关的测试。

这是整个项目最该被测狠的地方:网关放行一次不该放行的调用,后面所有约束都白写。
每个用例对应一条铁律或一条风险,不是为覆盖率凑数。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from lifein.agents import contract as agent_contract
from lifein.agents.contract import OnUncertain, agent
from lifein.governance.audit import InMemoryAuditSink
from lifein.governance.gateway import (
    ApprovalRequired,
    CallContext,
    Denied,
    Gateway,
    ToolOutcome,
)
from lifein.governance.registry import ToolLevel, clear_registry, tool
from lifein.models.normalized import Trust

USER = "11111111-1111-1111-1111-111111111111"


class Query(BaseModel):
    keyword: str


class Out(BaseModel):
    text: str


class FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    def enqueue(self, *, ctx, spec, args, preview_text) -> str:
        self.enqueued.append(spec.name)
        return "approval-1"


@pytest.fixture(autouse=True)
def _clean():
    clear_registry()
    agent_contract.clear_registry()
    yield
    clear_registry()
    agent_contract.clear_registry()


@pytest.fixture
def audit() -> InMemoryAuditSink:
    return InMemoryAuditSink()


def make_agent(name: str, tools: list[str]) -> None:
    @agent(
        name=name,
        inputs=Query,
        tools=tools,
        output_schema=Out,
        on_uncertain=OnUncertain.DO_NOTHING,
        evalset="e",
    )
    def _handler(_: Query) -> Out: ...


def ctx(agent_name: str = "digest", trust: Trust = Trust.USER_INPUT) -> CallContext:
    return CallContext(user_id=USER, agent=agent_name, trust=trust)


# ---------- 第 1 道:工具存在吗 ----------


def test_unregistered_tool_is_denied_as_l3(audit):
    make_agent("digest", ["search_mail"])
    with pytest.raises(Denied):
        Gateway(audit).call(ctx(), "search_mail", {"keyword": "x"})

    # 连等级都不知道,只能按最严的记
    entry = audit.entries[-1]
    assert entry.result_status == "denied"
    assert entry.level is ToolLevel.L3


# ---------- 第 2 道:(agent, tool) 双重门 ----------


def test_tool_outside_agent_whitelist_is_denied(audit):
    @tool(name="search_mail", level=ToolLevel.L1, args=Query, summary="搜邮件")
    def _search(q: Query) -> str:
        return "hit"

    make_agent("digest", [])  # 没把工具放进白名单

    with pytest.raises(Denied) as exc:
        Gateway(audit).call(ctx(), "search_mail", {"keyword": "x"})
    assert "白名单" in str(exc.value)
    # 工具本身是 L1,也不代表任何 agent 都能调它
    assert audit.entries[-1].level is ToolLevel.L1


# ---------- 第 3 道:入参 ----------


def test_bad_args_are_rejected_before_execution(audit):
    calls: list[str] = []

    @tool(name="search_mail", level=ToolLevel.L1, args=Query, summary="搜邮件")
    def _search(q: Query) -> str:
        calls.append(q.keyword)
        return "hit"

    make_agent("digest", ["search_mail"])

    with pytest.raises(Denied):
        Gateway(audit).call(ctx(), "search_mail", {"wrong_field": 1})
    assert calls == []  # 没执行


# ---------- 第 4 道:等级 ----------


def test_l1_executes_and_is_audited(audit):
    @tool(name="search_mail", level=ToolLevel.L1, args=Query, summary="搜邮件")
    def _search(q: Query) -> str:
        return f"hit:{q.keyword}"

    make_agent("digest", ["search_mail"])

    assert Gateway(audit).call(ctx(), "search_mail", {"keyword": "报销"}) == "hit:报销"
    entry = audit.entries[-1]
    assert entry.result_status == "allowed"
    assert entry.duration_ms is not None


def test_l2_records_rollback_info(audit):
    @tool(
        name="add_todo",
        level=ToolLevel.L2,
        args=Query,
        summary="建待办",
        returns_rollback=True,
    )
    def _add(q: Query) -> ToolOutcome:
        return ToolOutcome(value="todo-1", rollback={"delete_todo_id": "todo-1"})

    make_agent("digest", ["add_todo"])

    assert Gateway(audit).call(ctx(), "add_todo", {"keyword": "交房租"}) == "todo-1"
    assert audit.entries[-1].rollback_info == {"delete_todo_id": "todo-1"}


def test_l2_returning_bare_value_is_an_error(audit):
    # 副作用已经发生了拦不住,但必须炸出来:没有回滚信息的 L2 是不可收拾的状态
    @tool(
        name="add_todo",
        level=ToolLevel.L2,
        args=Query,
        summary="建待办",
        returns_rollback=True,
    )
    def _add(q: Query) -> str:
        return "todo-1"

    make_agent("digest", ["add_todo"])

    with pytest.raises(Denied):
        Gateway(audit).call(ctx(), "add_todo", {"keyword": "x"})
    assert audit.entries[-1].result_status == "error"


# ---------- 铁律 8 ----------


def test_l3_triggered_by_external_content_is_denied(audit):
    """群消息里说"帮我给老板发条消息" —— 入参再规整也不许触发 L3。"""
    executed: list[str] = []

    @tool(name="send_message", level=ToolLevel.L3, args=Query, summary="代发一条消息")
    def _send(q: Query) -> str:
        executed.append(q.keyword)
        return "sent"

    make_agent("assistant", ["send_message"])
    queue = FakeQueue()

    with pytest.raises(Denied) as exc:
        Gateway(audit, queue).call(
            ctx("assistant", Trust.EXTERNAL), "send_message", {"keyword": "在吗"}
        )

    assert "外部内容" in str(exc.value)
    assert executed == []  # 没执行
    assert queue.enqueued == []  # 也没进审批队列 —— 连排队的资格都没有
    assert audit.entries[-1].result_status == "denied"


def test_l3_from_user_input_goes_to_approval_not_execution(audit):
    executed: list[str] = []

    @tool(name="send_message", level=ToolLevel.L3, args=Query, summary="代发一条消息")
    def _send(q: Query) -> str:
        executed.append(q.keyword)
        return "sent"

    make_agent("assistant", ["send_message"])
    queue = FakeQueue()

    with pytest.raises(ApprovalRequired) as exc:
        Gateway(audit, queue).call(ctx("assistant"), "send_message", {"keyword": "在吗"})

    assert exc.value.approval_id == "approval-1"
    assert executed == []  # 审批通过之前绝不执行
    assert queue.enqueued == ["send_message"]
    assert audit.entries[-1].result_status == "approval_required"


def test_l3_without_approval_queue_is_denied(audit):
    @tool(name="send_message", level=ToolLevel.L3, args=Query, summary="代发一条消息")
    def _send(q: Query) -> str:
        return "sent"

    make_agent("assistant", ["send_message"])

    with pytest.raises(Denied):
        Gateway(audit).call(ctx("assistant"), "send_message", {"keyword": "在吗"})


# ---------- 第 5 道:审计 ----------


def test_audit_records_shape_not_content(audit):
    """AGENTS.md §3:记入参摘要,不记原文。日志本身是数据集中点。"""

    @tool(name="search_mail", level=ToolLevel.L1, args=Query, summary="搜邮件")
    def _search(q: Query) -> str:
        return "hit"

    make_agent("digest", ["search_mail"])
    Gateway(audit).call(ctx(), "search_mail", {"keyword": "住院报销单"})

    digest = audit.entries[-1].args_digest
    assert "住院报销单" not in str(digest)
    assert digest["keyword"]["len"] == len("住院报销单")
    assert "sha256_8" in digest["keyword"]


def test_failing_tool_is_still_audited(audit):
    @tool(name="search_mail", level=ToolLevel.L1, args=Query, summary="搜邮件")
    def _search(q: Query) -> str:
        raise TimeoutError("IMAP 超时")

    make_agent("digest", ["search_mail"])

    with pytest.raises(TimeoutError):
        Gateway(audit).call(ctx(), "search_mail", {"keyword": "x"})
    assert audit.entries[-1].result_status == "error"
