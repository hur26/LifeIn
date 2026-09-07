"""agent 注册表与五项契约。

ADR-013:输入类型、工具白名单、输出 schema、失败行为、评测集,**缺一项不许注册**。
P0 只有摘要和问答两个 agent,但机制要一次做对 —— 第三个 agent 进来时才补,
就得回头改前两个(03 P0 范围)。

这里没有编排框架,也不打算有(ADR-002)。所谓"注册"就是一份声明加一次校验,
它带来的三件事是:网关能做 (agent, tool) 双重门、审计能记清是谁调的、
评测有统一入口。除此之外它不参与运行。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from lifein.governance.registry import UnknownTool, get_tool


class OnUncertain(StrEnum):
    """拿不准时干什么。**没有默认值** —— 每个 agent 必须自己写明。

    铁律 7 说默认行为是"不动作",但"不动作"具体是哪一种要分清:
    什么都不做、进待确认队列、还是降级输出一个更保守的结果。
    """

    DO_NOTHING = "do_nothing"
    """什么都不做,也不打扰用户。适合摘要这类"少说一句无所谓"的场景。"""

    PENDING_CONFIRMATION = "pending_confirmation"
    """写进 pending_confirmations 等人确认(06 §2.7)。适合记账、日程提取。"""

    DEGRADE = "degrade"
    """输出一个更保守的结果并标注。适合问答:答不确定好过瞎编。"""


class ContractError(RuntimeError):
    """契约不完整或不成立。在导入期或启动校验时抛,不在运行中抛。"""


@dataclass(frozen=True)
class AgentSpec:
    name: str
    inputs: type[BaseModel]
    """输入类型。agent 之间靠类型对接,不靠"传个 dict 你自己看着办"。"""

    tools: frozenset[str]
    """工具白名单。网关按 (agent, tool) 双重门放行 —— 工具本身是 L1,
    也不代表任何 agent 都能调它。"""

    output_schema: type[BaseModel]
    """输出 schema。由 Pydantic 校验,这是 FastAPI 那套模型在本项目的第二个用处。"""

    on_uncertain: OnUncertain
    evalset: str
    """评测集标识(相对仓库根的路径)。

    **格式尚未定义**,记在 06 §4,P0 写摘要 agent 时定完回填。
    但"必须声明"这件事现在就生效 —— 先占位,免得第三个 agent 进来时才补。
    """

    handler: Callable[..., Any]


_REGISTRY: dict[str, AgentSpec] = {}


def agent(
    *,
    name: str,
    inputs: type[BaseModel],
    tools: Iterable[str],
    output_schema: type[BaseModel],
    on_uncertain: OnUncertain,
    evalset: str,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册一个 agent。五个关键字参数都没有默认值,这就是"缺一项不许注册"。"""

    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        if name in _REGISTRY:
            raise ContractError(f"agent 名重复:{name}")
        if not evalset.strip():
            raise ContractError(f"{name} 没声明评测集。格式未定不是不声明的理由")
        _REGISTRY[name] = AgentSpec(
            name=name,
            inputs=inputs,
            tools=frozenset(tools),
            output_schema=output_schema,
            on_uncertain=on_uncertain,
            evalset=evalset,
            handler=handler,
        )
        return handler

    return decorator


def get_agent(name: str) -> AgentSpec:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ContractError(f"未注册的 agent:{name}") from exc


def registered_agents() -> dict[str, AgentSpec]:
    return dict(_REGISTRY)


def validate_all() -> None:
    """启动期校验:每个 agent 白名单里的工具都得真实存在。

    不在注册时就校验,是因为导入顺序不可控 —— agent 模块可能先于工具模块导入。
    放到启动期做,代价是晚几十毫秒,换来的是不用关心 import 顺序。
    """
    problems: list[str] = []
    for spec in _REGISTRY.values():
        for tool_name in sorted(spec.tools):
            try:
                get_tool(tool_name)
            except UnknownTool:
                problems.append(f"agent {spec.name} 声明了不存在的工具 {tool_name}")
    if problems:
        raise ContractError("；".join(problems))


def clear_registry() -> None:
    """测试专用。"""
    _REGISTRY.clear()
