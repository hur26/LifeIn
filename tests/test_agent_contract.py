"""agent 契约的测试。

契约的价值全在"缺一项就注册不上"。能绕过去的契约等于没有。
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel

from lifein.agents import contract as agent_contract
from lifein.agents.contract import (
    ContractError,
    OnUncertain,
    agent,
    get_agent,
    validate_all,
)
from lifein.governance.registry import (
    ToolLevel,
    clear_registry,
    restore_registry,
    snapshot_registry,
    tool,
)


class In(BaseModel):
    day: str


class Out(BaseModel):
    text: str


class NoArgs(BaseModel):
    pass


@pytest.fixture(autouse=True)
def _clean():
    """**清空之后要还回去。**

    "导入即注册":`@tool` / `@agent` 只在模块第一次被 import 时跑一次。
    清完不还,整个进程里的注册表就一直是空的 —— 而后面哪些测试会红
    取决于文件名的字母顺序,那是最难查的一类红。
    """
    tools, agents = snapshot_registry(), agent_contract.snapshot_registry()
    clear_registry()
    agent_contract.clear_registry()
    yield
    restore_registry(tools)
    agent_contract.restore_registry(agents)


def register_tool(name: str) -> None:
    @tool(name=name, level=ToolLevel.L1, args=NoArgs, summary="只读")
    def _t() -> None: ...


def test_all_five_parts_are_required():
    """五项都没有默认值 —— 少写一个就是 TypeError,不是"用默认值凑合"。"""
    params = inspect.signature(agent).parameters
    required = {"name", "inputs", "tools", "output_schema", "on_uncertain", "evalset"}
    assert required <= set(params)
    for key in required:
        assert params[key].default is inspect.Parameter.empty, f"{key} 不该有默认值"


def test_registered_agent_is_retrievable():
    register_tool("list_mail")

    @agent(
        name="digest",
        inputs=In,
        tools=["list_mail"],
        output_schema=Out,
        on_uncertain=OnUncertain.DO_NOTHING,
        evalset="evals/digest.jsonl",
    )
    def _digest(_: In) -> Out:
        return Out(text="")

    spec = get_agent("digest")
    assert spec.tools == frozenset({"list_mail"})
    assert spec.on_uncertain is OnUncertain.DO_NOTHING


def test_empty_evalset_is_rejected():
    # 格式未定不是不声明的理由
    with pytest.raises(ContractError):

        @agent(
            name="x",
            inputs=In,
            tools=[],
            output_schema=Out,
            on_uncertain=OnUncertain.DO_NOTHING,
            evalset="  ",
        )
        def _x(_: In) -> Out: ...


def test_duplicate_agent_name_is_rejected():
    def make(name: str):
        @agent(
            name=name,
            inputs=In,
            tools=[],
            output_schema=Out,
            on_uncertain=OnUncertain.DEGRADE,
            evalset="e",
        )
        def _a(_: In) -> Out: ...

    make("same")
    with pytest.raises(ContractError):
        make("same")


def test_validate_all_catches_typo_in_tool_whitelist():
    @agent(
        name="digest",
        inputs=In,
        tools=["list_mial"],  # 打错了
        output_schema=Out,
        on_uncertain=OnUncertain.DO_NOTHING,
        evalset="e",
    )
    def _digest(_: In) -> Out: ...

    with pytest.raises(ContractError) as exc:
        validate_all()
    assert "list_mial" in str(exc.value)


def test_validate_all_passes_when_tools_exist():
    register_tool("list_mail")

    @agent(
        name="digest",
        inputs=In,
        tools=["list_mail"],
        output_schema=Out,
        on_uncertain=OnUncertain.DO_NOTHING,
        evalset="e",
    )
    def _digest(_: In) -> Out: ...

    validate_all()
