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


def test_there_are_repo_functions_to_check():
    # 防止上面的遍历因为改包结构而悄悄变成空集合,让这组测试假装通过
    assert list(public_functions())


def test_every_repo_function_takes_user_id_first():
    for qualname, func in public_functions():
        params = list(inspect.signature(func).parameters)
        assert params and params[0] == "user_id", f"{qualname} 的第一个参数不是 user_id"


def test_no_repo_function_defaults_user_id():
    # 有默认值就意味着"可以不传",而不传的那次就是串数据的那次
    for qualname, func in public_functions():
        first = next(iter(inspect.signature(func).parameters.values()))
        assert first.default is inspect.Parameter.empty, f"{qualname} 的 user_id 不该有默认值"
