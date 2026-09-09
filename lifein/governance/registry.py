"""工具注册表。

架构 §9.2:**声明即被网关接管**,自动获得分权、审计、成本记录。
铁律 3:**未声明等级的工具默认按 L3 拒绝** —— 新增工具忘记标注不会造成越权,
只会造成"这个工具用不了",这是刻意选的失败方向。

注册表是能力层与治理层之间唯一的接缝。编排层拿不到函数本身,只能拿到名字,
所以它绕不过网关(架构 §1)。这条靠的不是约定,是这里不导出函数引用。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from functools import wraps
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
    channel: Any = None
    """推送出口。**只有 L3 的代发消息用得上**,别的工具留 None。

    工具自己不去装配通道,和"工具自己不开事务"是同一条理由:一个能自己决定
    走哪条通道的工具,审计里记下的通道名就可能是假的 —— 而"这条消息从哪儿发的"
    正是出事之后唯一要查的东西。
    """


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

    preview: Callable[[Any], str] | None = None
    """把这次调用的参数写成一句人话。**L3 必须有。**

    `summary` 是静态的("代发一条消息"),而审批卡片上要看到的是
    **这一次**要做什么("给老王发:明天三点见")。
    [03 的退出条件](../../docs/03-roadmap.md#退出条件-2)写着
    "你自己不敢点'同意' → 预览做得不够清楚" —— 把 `tool_args` 的 JSON 打上去
    是能跑的,但那时你点同意是在赌,而不是在判断。
    """

    def preview_for(self, args: Any) -> str:
        """这一次调用的人话。没有 `preview` 就退回 `summary`(只有 L1/L2 会走到)。"""
        if self.preview is None:
            return self.summary
        text = self.preview(args).strip()
        return text or self.summary


_REGISTRY: dict[str, ToolSpec] = {}


class DirectCall(RuntimeError):
    """有人绕过网关直接调了工具函数([铁律 4](../../AGENTS.md#1-铁律))。

    **这不是"用法错了",是一条防线被跳过了。** 跳过网关意味着这一次调用
    没查等级、没查白名单、没记审计、L3 也没进审批队列 —— 而这四样正是
    治理层存在的全部理由。
    """


_INSIDE_GATEWAY: ContextVar[str | None] = ContextVar("lifein_tool_call", default=None)
"""当前正在被治理层执行的工具名。**没有它的话"过网关"和"不过网关"
在代码里长得一模一样** —— 两者都是 `func(args, ctx)`。

用 `ContextVar` 不用全局变量:它跟着调用栈走,而且在 asyncio 任务之间隔离。
"""


@contextmanager
def executing(tool_name: str) -> Iterator[None]:
    """**我知道我在绕过网关,而这一次是有理由的。**

    名字取得这么直白是有意的:铁律 4 在 Python 里做不到"不可能绕过",
    只能做到"绕过需要显式写出来,而且写出来会被看见"。谁 import 了它,
    `tests/test_layering.py` 会看见。

    **只有两个地方该用它:**

    - `governance/gateway.py` —— 正门
    - `jobs/approval_execute.py` —— L3 的执行。那次调用在**进审批队列那一刻**
      已经过了网关(等级、白名单、入参、trust 全查过了),人点同意之后
      再过一次网关只会把它重新变成一条审批
    """
    token = _INSIDE_GATEWAY.set(tool_name)
    try:
        yield
    finally:
        _INSIDE_GATEWAY.reset(token)


def _guarded(name: str, func: Callable[..., Any]) -> Callable[..., Any]:
    """把工具函数包一层,进门先看是不是从治理层来的。

    比对的是**工具名**不是一个布尔值:否则在 `a` 的网关调用里顺手调一下 `b`
    也能过 —— 而那正是"绕过网关"最像正常代码的一种写法。
    """

    @wraps(func)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        if _INSIDE_GATEWAY.get() != name:
            raise DirectCall(
                f"{name} 被直接调用了,没过治理层网关(铁律 4)。"
                "要调它请走 Gateway.call();"
                "确实需要绕过的地方只有审批执行,那里用 registry.executing()"
            )
        return func(*args, **kwargs)

    return guarded


def tool(
    *,
    name: str,
    level: ToolLevel,
    args: type[BaseModel],
    summary: str,
    returns_rollback: bool = False,
    preview: Callable[[Any], str] | None = None,
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
        if level is ToolLevel.L3 and preview is None:
            # 和上面 L2 那条同一个道理:在导入期就炸,而不是等第一张审批卡片
            # 发出去时才发现上面是一坨 JSON。**那时你会点同意,因为看不懂**
            raise ToolError(
                f"{name} 是 L3,必须给 preview —— 审批卡片上要写清这一次要做什么,"
                "而不是这个工具一般是做什么的"
            )
        _REGISTRY[name] = ToolSpec(
            name=name,
            level=level,
            args_model=args,
            # **注册表里存的是包过的那一份。** 存原函数的话,拿 `spec.func`
            # 调一样绕得过去 —— 而那是最容易发生的绕法,因为它看起来
            # 完全像在用注册表
            func=_guarded(name, func),
            summary=summary,
            returns_rollback=returns_rollback,
            preview=preview,
        )
        # **返回包过的那一份,不是原函数。** 返回原函数的话,
        # `from lifein.tools.todo import create` 拿到的就是没有守卫的版本 ——
        # 而那正是铁律 4 要挡的那一句
        return _REGISTRY[name].func

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
    """清空注册表。**测试专用**,生产代码调它一定是哪里错了。

    **清完要还回去**,用下面两个。原因是"导入即注册":`@tool` 只在模块第一次
    被 import 时跑一次,清空之后再调 `bootstrap.register_tools()` 什么都不会
    发生 —— 模块早就在 `sys.modules` 里了。于是清过的那个测试文件之后,
    整个进程里的注册表就是空的,而**后面哪些测试会红取决于文件名的字母顺序**。
    """
    _REGISTRY.clear()


def snapshot_registry() -> dict[str, ToolSpec]:
    """测试专用:把当前注册表存一份,配合 `restore_registry` 用。"""
    return dict(_REGISTRY)


def restore_registry(snapshot: dict[str, ToolSpec]) -> None:
    """测试专用:还原成 `snapshot_registry()` 拿到的那份。"""
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)
