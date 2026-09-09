"""铁律 4:**能力层的工具不许被编排层直接调用,必须过治理层网关。**

那条铁律最后半句是"**这在代码结构上强制,不靠约定**",而在这一组测试存在
之前,它恰恰只是约定:`from lifein.tools.transaction import record` 然后直接调,
注册表拦不住,网关也不知道 —— 那一次调用没查等级、没查白名单、没记审计,
L3 也没进审批队列。

三道防线,这个文件是第二道和第三道:

| 哪一道 | 在哪 | 什么时候发现 |
| --- | --- | --- |
| 运行时守卫 | `registry._guarded` | 真调下去的那一刻,抛 `DirectCall` |
| **导入检查** | 这里 | 跑测试时 —— **连写出来的机会都没有** |
| **例外清单** | 这里 | 有人给自己开了绕过网关的口子时 |

**在 Python 里做不到"不可能绕过"**,只能做到"绕过需要显式地写出来,
而且写出来会被看见"。这一组要的就是后者。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIFEIN = ROOT / "lifein"

ORCHESTRATION = ("agents", "jobs")
"""编排层。这两个包里的模块**一个都不许 import 能力层**。

`api` 不在里面:它有一处合法的直接调用 —— 确认待确认队列时照 payload 写入
(06 §6.6 那条"唯一的例外"),而那一处**也是过网关的**,只是调用方是
用户的点击而不是 agent。它 import 的是 `governance.gateway`,不是 `tools`,
所以下面第一条同样管得住它。
"""

ALLOWED_TO_BYPASS = {
    # 正门。
    "lifein/governance/gateway.py",
    # L3 的执行。那次调用在**进审批队列那一刻**已经过了网关 ——
    # 等级、白名单、入参、trust 全查过了,人点同意之后再过一次网关,
    # 只会把它重新变成一条审批。
    "lifein/jobs/approval_execute.py",
}
"""允许 import `registry.executing`(那个"我知道我在绕过网关"的上下文)的文件。

**加一个进来之前先回答一个问题:那次调用是什么时候过的网关?**
答不上来的话,它要的不是绕过,是 `Gateway.call()`。
"""


def python_files(*packages: str) -> list[Path]:
    return [
        path
        for package in packages
        for path in (LIFEIN / package).rglob("*.py")
        if "__pycache__" not in path.parts
    ]


def imported_modules(path: Path) -> set[str]:
    """这个文件 import 了哪些模块(含 `from x import y` 里的 `x`)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_orchestration_never_imports_the_capability_layer():
    """**编排层 import 能力层 = 铁律 4 被绕过的那一行代码。**

    红了的时候不要把这个文件加进白名单 —— 先问:这个调用为什么不能走
    `Gateway.call()`?十有八九是能的,而不能的那种情况见
    `ALLOWED_TO_BYPASS` 上面那段。
    """
    offenders: list[str] = []
    for path in python_files(*ORCHESTRATION):
        for module in imported_modules(path):
            if module.startswith("lifein.tools"):
                offenders.append(f"{path.relative_to(ROOT).as_posix()} → {module}")

    assert offenders == [], (
        "编排层直接 import 了能力层(铁律 4):\n  "
        + "\n  ".join(offenders)
        + "\n工具要过 Gateway.call() —— 直接调等于跳过等级、白名单、审计和审批"
    )


def test_only_the_gateway_and_approval_execution_can_bypass():
    """`registry.executing` 是**唯一**一条合法的绕过通道,而它有两个用户。

    这一条比上面那条更要紧:上面那条挡的是"顺手 import 了工具",
    这一条挡的是"**给自己开了一个口子**" —— 后者是明知故犯,
    也因此更需要一个会说话的地方。
    """
    offenders = []
    for path in python_files("agents", "jobs", "api", "governance", "sources", "repos"):
        name = path.relative_to(ROOT).as_posix()
        if name in ALLOWED_TO_BYPASS:
            continue
        if any(m.endswith("registry.executing") for m in imported_modules(path)):
            offenders.append(name)

    assert offenders == [], (
        "这些文件给自己开了绕过网关的口子:\n  "
        + "\n  ".join(offenders)
        + "\n先回答:那次调用是什么时候过的网关?答不上来就该走 Gateway.call()"
    )


def test_the_two_allowed_files_still_exist():
    """白名单会**因为文件改名而静默失效** —— 那时上面那条永远是绿的,
    而绕过通道谁都能开。"""
    for name in ALLOWED_TO_BYPASS:
        assert (ROOT / name).exists(), f"{name} 不在了,白名单该跟着改"


class TestTheRuntimeGuard:
    """第一道:真调下去的那一刻。

    导入检查挡不住反射(`get_tool(name).func`),而那恰恰是最像正常代码的
    一种绕法 —— 它看起来完全像在用注册表。
    """

    @pytest.fixture(autouse=True)
    def _registered(self):
        from lifein.bootstrap import register_tools

        register_tools()

    def test_calling_through_the_registry_is_blocked(self):
        from lifein.governance.registry import DirectCall, get_tool

        spec = get_tool("todo.create")
        with pytest.raises(DirectCall):
            spec.func(None, None)

    def test_importing_the_function_gives_you_the_guarded_one(self):
        """**装饰器返回的是包过的那一份。**

        返回原函数的话,`from lifein.tools.todo import create` 拿到的就是
        没有守卫的版本 —— 而那正是铁律 4 要挡的那一句。
        """
        from lifein.governance.registry import DirectCall
        from lifein.tools.todo import create

        with pytest.raises(DirectCall):
            create(None, None)

    def test_the_context_is_per_tool_not_a_flag(self):
        """比对的是**工具名**不是一个布尔值。

        否则在 `a` 的网关调用里顺手调一下 `b` 也能过 —— 而那正是
        "绕过网关"最像正常代码的一种写法。
        """
        from lifein.governance.registry import DirectCall, executing, get_tool

        with executing("todo.create"), pytest.raises(DirectCall):
            get_tool("txn.record").func(None, None)

    def test_the_guard_does_not_swallow_the_real_error(self):
        """守卫放行之后,工具自己抛的错要原样出来 ——
        包一层最容易顺手做错的就是这个。"""
        from lifein.governance.registry import executing, get_tool

        with executing("todo.create"), pytest.raises(Exception) as caught:
            get_tool("todo.create").func(None, None)
        assert "铁律 4" not in str(caught.value)
