"""数据访问层的契约检查。

**铁律 1 靠这组测试盯着。** "所有数据访问函数第一个参数是 user_id"是那种
一旦违反就没有任何报错、只会静默串数据的规则 —— 靠人记不住,靠 review
也会漏,所以用签名检查。

新加一个 repo 函数忘了带 user_id,这里会红。
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import lifein.repos


def public_functions():
    """遍历 lifein.repos 下所有模块的公开函数。"""
    for module_info in pkgutil.iter_modules(lifein.repos.__path__):
        module = importlib.import_module(f"lifein.repos.{module_info.name}")
        for name, obj in vars(module).items():
            if name.startswith("_") or not inspect.isfunction(obj):
                continue
            if obj.__module__ != module.__name__:  # 跳过 import 进来的
                continue
            yield f"{module_info.name}.{name}", obj


def data_access_functions():
    """真正碰数据库的那些 —— 判据是**它收不收 session**。

    按"在不在 repos 包里"划会误伤:push_log.digest_card 是个纯函数,
    它定义的是往那一列里存什么形状,不访问任何数据。
    拿不到 session 就做不了数据访问,所以这个判据不会漏。

    模块自己声明的 `NO_USER_ID_REQUIRED` 会被排除。目前只有 users 模块用到:
    身份解析那一步手上还没有 user_id,它正是要算出这个值。
    **例外必须写在代码里而不是这里** —— 加例外要改那个集合,那是个看得见的动作。
    """
    for module_info in pkgutil.iter_modules(lifein.repos.__path__):
        module = importlib.import_module(f"lifein.repos.{module_info.name}")
        exempt = getattr(module, "NO_USER_ID_REQUIRED", frozenset())
        for name, obj in vars(module).items():
            if name.startswith("_") or not inspect.isfunction(obj):
                continue
            if obj.__module__ != module.__name__:
                continue
            if name in exempt:
                continue
            if "session" in inspect.signature(obj).parameters:
                yield f"{module_info.name}.{name}", obj


def test_exemptions_are_narrow():
    """例外只允许出现在 users 模块,且只有身份解析与建用户两类。

    这条测试存在的意义是让"再破一次例"变得需要解释:哪天有人想给别的模块
    加 NO_USER_ID_REQUIRED,得先改这里。
    """
    for module_info in pkgutil.iter_modules(lifein.repos.__path__):
        module = importlib.import_module(f"lifein.repos.{module_info.name}")
        exempt = getattr(module, "NO_USER_ID_REQUIRED", frozenset())
        if not exempt:
            continue
        assert module_info.name == "users", f"{module_info.name} 不该有例外"
        assert exempt == {"find_by_wecom_userid", "create_user"}


def test_there_are_data_access_functions_to_check():
    # 防止遍历因为改包结构而悄悄变成空集合,让这组测试假装通过
    assert list(data_access_functions())


def test_every_data_access_function_takes_user_id_first():
    for qualname, func in data_access_functions():
        params = list(inspect.signature(func).parameters)
        assert params and params[0] == "user_id", f"{qualname} 的第一个参数不是 user_id"


def test_no_data_access_function_defaults_user_id():
    # 有默认值就意味着"可以不传",而不传的那次就是串数据的那次
    for qualname, func in data_access_functions():
        first = next(iter(inspect.signature(func).parameters.values()))
        assert first.default is inspect.Parameter.empty, f"{qualname} 的 user_id 不该有默认值"


def test_session_is_the_second_parameter():
    """签名统一成 (user_id, session, *, ...)。

    统一不是洁癖:调用点全是 `repo.xxx(user_id, session, ...)` 的形状,
    多出来的那个位置参数一眼就能看出是不是传错了。
    """
    for qualname, func in data_access_functions():
        params = list(inspect.signature(func).parameters)
        assert params[1] == "session", f"{qualname} 的第二个参数不是 session"
