"""导出与彻底注销(P4 第 6 片)。

[09 隐私说明](../../docs/09-privacy.md)第 5 节写着两件"要找白杨的"事:
**彻底注销账户**、**导出全部数据的归档文件**。这个模块是那两件。

## 为什么这两件不做进 App

不是做不了,是**做进去会让另一件事变危险**。App 里的删除按钮删的是采集数据
(P4 第 3 片),那是可以随手点的 —— 点错了明天重新采就有了。
而"彻底注销"删的是账号本身:凭据、待办、记忆、账本,全部。

**一个随手能点的注销按钮,和一个随手能点的采集开关,长得太像了。**
所以注销留在人对人那一步:你说一声,我做,而做之前我会再确认一次。

## 导出要能被别的东西读

JSON,一个用户一份,里面按表分节。**不做成"可以再导入回来"的格式** ——
那会让人以为它是备份,而备份是另一件事(P4 第 7 片,有自己的演练要求)。
它的用途是回答"我的数据都有什么",以及在你想走的时候把它带走。

**凭据不导出。** 导出文件会躺在下载目录、会被发到微信 —— 而里面如果有
邮箱授权码,那份文件的危险程度就超过了它保护的东西。凭据在库里是加密的,
导出成明文等于把加密那一步作废。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

EXPORTED_TABLES = (
    "raw_events",
    "transactions",
    "todos",
    "facts",
    "entities",
    "entity_aliases",
    "pending_confirmations",
    "approvals",
    "budgets",
    "merchant_rules",
    "collector_whitelist",
    "rule_state",
    "push_log",
    "tool_calls",
    "job_runs",
)
"""导出哪些表。**`credentials` 和 `enrollment_codes` 不在里面**(见模块开头)。

`embeddings` 也不在:它是 `facts` 的派生向量,导出来没有人读得懂,
而且它比原文还大。
"""

DELETED_TABLES = (
    # 顺序要紧:有外键指向 raw_events 的先删
    "embeddings",
    "transactions",
    "pending_confirmations",
    "approvals",
    "todos",
    "facts",
    "entity_aliases",
    "entities",
    "raw_events",
    "budgets",
    "merchant_rules",
    "collector_whitelist",
    "rule_state",
    "push_log",
    "tool_calls",
    "job_runs",
    "channel_state",
    "enrollment_codes",
    "credentials",
)
"""注销时删哪些表,**按外键顺序**。

反了的话数据库直接拒绝,而那个报错("violates foreign key constraint")
完全不解释应该先删哪一个 —— 和 `data_control.delete_collected` 踩过的是同一个坑。

`users` 不在里面:它由调用方最后删,因为**删到一半失败时,留着 `users` 那一行
才知道这个账号处理了一半**。
"""


@dataclass(frozen=True)
class Export:
    user_id: str
    exported_at: datetime
    tables: dict[str, list[dict[str, Any]]]

    def row_count(self) -> int:
        return sum(len(rows) for rows in self.tables.values())


def export_user(user_id: str, session: Session, *, now: datetime) -> Export:
    """把这个用户的数据导出来。**凭据不在里面。**

    每张表一条 `SELECT * WHERE user_id = ...` —— 用 `SELECT *` 而不是列清单,
    是因为**加了列忘了改这里的话,导出会静默地少一列**,
    而少一列的导出文件看起来完全正常。
    """
    tables: dict[str, list[dict[str, Any]]] = {}
    for table in EXPORTED_TABLES:
        rows = session.execute(
            text(f"SELECT * FROM {table} WHERE user_id = :user_id"),  # noqa: S608
            {"user_id": user_id},
        ).mappings()
        tables[table] = [_jsonable(dict(row)) for row in rows]

    log.info("导出完成:user=%s 共 %s 行", user_id, sum(len(v) for v in tables.values()))
    return Export(user_id=user_id, exported_at=now, tables=tables)


def purge_user(user_id: str, session: Session) -> dict[str, int]:
    """彻底删掉这个用户的全部数据。**不删 `users` 那一行。**

    留着它是刻意的:删到一半失败时,**留着 `users` 才知道这个账号处理了一半**。
    调用方在确认全部删干净之后自己删那一行(或者标 `disabled_at`)。

    返回每张表删了几行 —— 而**那个数字要给人看**:注销是一次性的,
    做完之后唯一能回答"真的删了吗"的就是它。
    """
    removed: dict[str, int] = {}
    for table in DELETED_TABLES:
        count = session.execute(
            text(f"DELETE FROM {table} WHERE user_id = :user_id"),  # noqa: S608
            {"user_id": user_id},
        ).rowcount
        if count:
            removed[table] = int(count)

    log.warning("已彻底删除用户数据:user=%s %s", user_id, removed)
    return removed


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    """把库里的类型变成 JSON 能表示的。

    **`Decimal` 转字符串不转 float**:JSON 的 number 是双精度浮点,
    `38.50` 会变成 `38.499999999999996` —— 而这份文件是给人看"我花了多少"的。

    UUID、日期、二进制也要转。**转不了的类型会让导出跑到一半才炸**,
    而那时你已经告诉人家"在导了" —— 所以宁可多列几种,也不要漏。
    """
    import uuid
    from datetime import date
    from decimal import Decimal

    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, uuid.UUID):
            # UUID 主键(users、entities、facts、credentials 都用它)。
            # **不转的话导出跑到一半才炸**,而那时你已经告诉人家"在导了"
            out[key] = str(value)
        elif isinstance(value, Decimal):
            out[key] = str(value)
        elif isinstance(value, datetime | date):
            out[key] = value.isoformat()
        elif isinstance(value, bytes):
            # ciphertext 那类字段。**不该出现在这里**(EXPORTED_TABLES 里没有
            # credentials),但万一将来加了表,不要静默地塞一段二进制进去
            out[key] = f"<{len(value)} 字节的二进制,未导出>"
        elif isinstance(value, memoryview):
            out[key] = f"<{len(value)} 字节的二进制,未导出>"
        else:
            out[key] = value
    return out
