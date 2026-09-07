"""工具注册表的测试。

注册表是治理层的入口,它松一个口子,后面的分权全是摆设。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from lifein.governance.registry import (
    ToolError,
    ToolLevel,
    UnknownTool,
    clear_registry,
    get_tool,
    registered_tools,
    tool,
)


class NoArgs(BaseModel):
    pass


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def test_registered_tool_is_retrievable():
    @tool(name="list_events", level=ToolLevel.L1, args=NoArgs, summary="列出今天的日程")
    def _list_events() -> list[str]:
        return []

    spec = get_tool("list_events")
    assert spec.level is ToolLevel.L1
    assert spec.summary == "列出今天的日程"


def test_unknown_tool_raises_instead_of_defaulting_to_allow():
    # 铁律 3:取不到不许兜底成"当作 L1 放行"
    with pytest.raises(UnknownTool):
        get_tool("没注册过的工具")


def test_duplicate_name_is_rejected():
    @tool(name="dup", level=ToolLevel.L1, args=NoArgs, summary="第一个")
    def _a() -> None: ...

    with pytest.raises(ToolError):

        @tool(name="dup", level=ToolLevel.L1, args=NoArgs, summary="第二个")
        def _b() -> None: ...


def test_l2_without_rollback_fails_at_import_time():
    # 不能等它半夜改错一条日历再发现没法回滚
    with pytest.raises(ToolError) as exc:

        @tool(name="update_calendar", level=ToolLevel.L2, args=NoArgs, summary="改日历")
        def _update() -> None: ...

    assert "回滚" in str(exc.value)


def test_l2_with_rollback_registers():
    @tool(
        name="update_calendar",
        level=ToolLevel.L2,
        args=NoArgs,
        summary="改日历",
        returns_rollback=True,
    )
    def _update() -> None: ...

    assert get_tool("update_calendar").returns_rollback is True


def test_empty_summary_is_rejected():
    # L3 的审批卡片要拿 summary 给人看,JSON 不是给人读的
    with pytest.raises(ToolError):

        @tool(name="send", level=ToolLevel.L3, args=NoArgs, summary="   ")
        def _send() -> None: ...


def test_snapshot_is_a_copy():
    @tool(name="x", level=ToolLevel.L1, args=NoArgs, summary="s")
    def _x() -> None: ...

    snapshot = registered_tools()
    snapshot.clear()
    assert get_tool("x") is not None  # 改快照不该影响注册表
