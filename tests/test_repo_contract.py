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


ALLOWED_EXEMPTIONS = {
    # 身份解析那一步手上还没有 user_id —— 它正是要算出这个值
    "users": {"find_by_wecom_userid", "create_user", "list_active_users"},
    # 配码同理(P4 第 1 片):App 扫码那一刻只有一张码,而"这张码是谁的"
    # 就存在那一行里。要求它先给 user_id,等于要求它先知道自己配的是谁的号。
    # peek 是 claim 的只读版,purge_expired 按时间扫全表 ——
    # **两者都不返回任何属于某个用户的数据**
    "enrollment": {"claim", "peek", "purge_expired"},
}
"""哪些模块允许破例,以及破哪几个。

**这份清单是这组测试真正的内容。** 上面那些签名检查任何时候都能通过 ——
只要有人给模块加一行 `NO_USER_ID_REQUIRED`。而加进这份清单要改这个文件,
那是一个看得见、要在 review 里解释的动作。
"""


def test_exemptions_are_narrow():
    """例外只允许出现在上面那份清单里,一个不多。

    这条测试存在的意义是让"再破一次例"变得需要解释:哪天有人想给别的模块
    加 NO_USER_ID_REQUIRED,得先改 ALLOWED_EXEMPTIONS。
    """
    for module_info in pkgutil.iter_modules(lifein.repos.__path__):
        module = importlib.import_module(f"lifein.repos.{module_info.name}")
        exempt = getattr(module, "NO_USER_ID_REQUIRED", frozenset())
        if not exempt:
            continue
        allowed = ALLOWED_EXEMPTIONS.get(module_info.name)
        assert allowed is not None, f"{module_info.name} 不在允许破例的清单里"
        assert set(exempt) == allowed, f"{module_info.name} 的例外和清单对不上"


def test_the_exemption_list_itself_stays_short():
    """**破例的模块只有两个。** 第三个出现时,要先回答"为什么这次也算特殊"。"""
    assert set(ALLOWED_EXEMPTIONS) == {"users", "enrollment"}


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
