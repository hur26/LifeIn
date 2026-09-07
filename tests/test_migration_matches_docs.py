"""迁移与文档的一致性。

06 说"改这里的任何一个字段,等于改代码"。这组测试是那句话的执行者:
文档里加了一张表而迁移没跟上,或者迁移里悄悄多出一张文档没有的表,都会红。

它不校验字段级别的差异 —— 那要写一个 SQL 解析器,不值得。它校验的是
**最容易忘的两件事**:表的增减,以及那三条安全约束还在不在。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VERSIONS = REPO / "migrations" / "versions"
DOCS = [REPO / "docs" / "06-data-model.md", REPO / "docs" / "07-config.md"]

CREATE_TABLE = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)")


def load_migration(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def all_migrations():
    """遍历全部迁移,不是只看 0001。

    只看第一份的话,后面每加一次迁移这组检查就少覆盖一块 —— 而它存在的意义
    正是"文档和迁移不许分家"。
    """
    return [load_migration(p) for p in sorted(VERSIONS.glob("0*.py"))]


def tables_in_docs() -> set[str]:
    found: set[str] = set()
    for doc in DOCS:
        found |= set(CREATE_TABLE.findall(doc.read_text(encoding="utf-8")))
    return found


def migration_ddls() -> list[str]:
    ddls: list[str] = []
    for module in all_migrations():
        # 有的迁移用 TABLES 列表,有的只建一张表用 TABLE
        ddls.extend(getattr(module, "TABLES", []))
        single = getattr(module, "TABLE", None)
        if single:
            ddls.append(single)
    return ddls


def tables_in_migration() -> set[str]:
    return {
        CREATE_TABLE.search(ddl).group(1) for ddl in migration_ddls() if CREATE_TABLE.search(ddl)
    }


def test_migration_covers_every_documented_table():
    missing = tables_in_docs() - tables_in_migration()
    assert not missing, f"文档里有但迁移没建:{sorted(missing)}"


def test_migration_has_no_undocumented_table():
    extra = tables_in_migration() - tables_in_docs()
    assert not extra, f"迁移里建了但文档没写:{sorted(extra)}"


def test_drop_order_covers_every_table():
    # downgrade 漏一张表,回滚后再 upgrade 就会撞上"已存在"
    dropped: set[str] = set()
    for module in all_migrations():
        dropped.update(getattr(module, "DROP_ORDER", []))
        single = getattr(module, "TABLE", None)
        if single and not hasattr(module, "DROP_ORDER"):
            # 单表迁移的 downgrade 直接写在函数里,从 DDL 反推表名
            dropped.add(CREATE_TABLE.search(single).group(1))
    assert dropped == tables_in_migration()


def test_drop_order_is_dependency_safe():
    # 被引用的表必须后删。引用关系从 REFERENCES 里读,不手写清单 ——
    # 手写的清单会在加新外键时忘记更新
    module = all_migrations()[0]
    position = {name: i for i, name in enumerate(module.DROP_ORDER)}
    for ddl in module.TABLES:
        table = CREATE_TABLE.search(ddl).group(1)
        for referenced in re.findall(r"REFERENCES (\w+)", ddl):
            if referenced == table:
                continue
            assert position[table] < position[referenced], (
                f"{table} 引用了 {referenced},必须排在它前面删"
            )


def test_security_constraints_are_in_the_schema():
    """这三条 CHECK 是铁律的执行者,不是普通的数据校验。

    它们一旦从 schema 里消失,对应的铁律就退回成"文档里的一句话",
    而提示注入骗过 agent 之后就再没有第二道闸门。
    """
    sql = "\n".join(migration_ddls())
    assert "facts_provenance_required" in sql  # 铁律 5
    assert "l3_never_triggered_by_external" in sql  # 铁律 8
    assert "l2_needs_rollback" in sql  # L2 必须可回滚


def test_every_business_table_has_user_id():
    """铁律 1:所有表带 user_id。P0 只有一个用户也不许省。"""
    for ddl in migration_ddls():
        table = CREATE_TABLE.search(ddl).group(1)
        if table == "users":  # 它自己就是 user_id 的出处
            continue
        assert re.search(r"^\s*user_id\s", ddl, re.MULTILINE), f"{table} 缺 user_id"
