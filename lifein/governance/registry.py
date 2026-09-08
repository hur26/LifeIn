"""工具注册表。

架构 §9.2:**声明即被网关接管**,自动获得分权、审计、成本记录。
铁律 3:**未声明等级的工具默认按 L3 拒绝** —— 新增工具忘记标注不会造成越权,
只会造成"这个工具用不了",这是刻意选的失败方向。

注册表是能力层与治理层之间唯一的接缝。编排层拿不到函数本身,只能拿到名字,
所以它绕不过网关(架构 §1)。这条靠的不是约定,是这里不导出函数引用。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session


class ToolLevel(StrEnum):
    """三级分权,见 docs/01-product-spec.md §7。"""

    L1 = "L1"
    """只读。自动放行,记审计日志。"""

    L2 = "L2"
    """写自己的地盘。自动放行,**必须可回滚**,记审计日志。"""

    L3 = "L3"
    """写外部世界。强制拦截进审批队列,人工确认后执行。"""


class ToolError(RuntimeError):
    """工具注册或查找出错。一律在导入期或调用前抛,不在执行中途抛。"""


class UnknownTool(ToolError):
    """要调的工具没注册。按铁律 3,这等同于 L3 拒绝。"""


@dataclass(frozen=True)
class ToolContext:
    """一次工具执行的上下文。**每个工具都收它,用不用随意。**

    `session` 由调用方给,**工具自己不许开事务**。这条不是风格问题:
    06 §2.7 要求"写入 target_table 与更新 `pending_confirmations.status`
    必须在同一个事务里",工具自己 `session_scope()` 一下就永远做不到 ——
    那是两个事务,中间断电就得到"确认了但没写进去"或者"写了两次"。

    P1 第一批工具是只读的 L1,其实用不上这条约束。但接口现在就定死,
    等 L2 上线时才加参数,已有的工具全要回头改一遍
    (P0 在 agent 契约上正是这么做的,收益已经验证过)。
    """

    user_id: str
    session: Session | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    level: ToolLevel
    args_model: type[BaseModel]
    """入参 schema。用 Pydantic 而不是裸 dict —— 校验失败要在调用前发生。"""

    func: Callable[..., Any]
    """签名固定是 `(args, ctx: ToolContext)`。两个参数都给,不做"可选第二参数"
    那种分支 —— 一个函数有两种调法,就一定会有人按错的那种写。"""

    summary: str
    """一句人话。L3 的审批卡片要拿它给用户看,JSON 不是给人读的。"""

    returns_rollback: bool
    """执行后是否返回回滚信息。L2 必须为 True,见 06 §2.9 的 CHECK 约束。"""


_REGISTRY: dict[str, ToolSpec] = {}


def tool(
    *,
    name: str,
    level: ToolLevel,
    args: type[BaseModel],
    summary: str,
    returns_rollback: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把一个函数注册成工具。

    `level` 没有默认值,是故意的:写不出等级说明还没想清楚这个工具动什么。
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if name in _REGISTRY:
            raise ToolError(f"工具名重复:{name}")
        if level is ToolLevel.L2 and not returns_rollback:
            # 在导入期就炸,而不是等它半夜改错一条日历再发现没法回滚。
            # 对应 tool_calls 表上的 l2_needs_rollback 约束。
            raise ToolError(f"{name} 是 L2,必须能返回回滚信息(returns_rollback=True)")
        if not summary.strip():
            raise ToolError(f"{name} 缺 summary。L3 审批卡片要拿它给人看")
        _REGISTRY[name] = ToolSpec(
            name=name,
            level=level,
            args_model=args,
            func=func,
            summary=summary,
            returns_rollback=returns_rollback,
        )
        return func

    return decorator


def get_tool(name: str) -> ToolSpec:
    """按名字取工具。取不到就是 UnknownTool —— 调用方不许兜底成"当作 L1 放行"。"""
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise UnknownTool(f"未注册的工具:{name}(按铁律 3 视同 L3 拒绝)") from exc


def registered_tools() -> dict[str, ToolSpec]:
    """只读快照。给 agent 契约校验和运维排查用。"""
    return dict(_REGISTRY)


def clear_registry() -> None:
    """清空注册表。**测试专用**,生产代码调它一定是哪里错了。"""
    _REGISTRY.clear()
