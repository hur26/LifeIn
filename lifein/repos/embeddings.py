"""`embeddings` 的读写 —— 模糊召回的那一半。

字面检索能回答"和张三的往来",回答不了"上次那个报销的事" —— 用户嘴里的
说法和事实里的措辞几乎不会一样。向量补的就是这一段。

三条规矩:

**同一个 `model` 之间才比距离。** 表上的 UNIQUE 是
`(user_id, ref_type, ref_id, model)`,换模型时新旧向量并存,查询**永远带上
model 条件**。两个模型的向量之间算余弦相似度会得到一个看起来很正常、
实际毫无意义的数 —— 那种错不会报警,只会让召回悄悄变差(06 §2.4)。

**没有向量不是故障。** `EMBEDDING_MODEL` 没配、embedding 接口挂了、
新写的事实还没来得及算 —— 这些时候召回返回空,调用方退回字面检索
(ADR-019)。记忆层不因为向量缺席而不可用。

**只存引用,不存原文。** `ref_type` + `ref_id` 指回 `facts` 或 `raw_events`,
正文一个字节都不复制到这张表里。多一份副本就多一处要删干净的地方。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


class RefType(StrEnum):
    """向量指向什么。取值同 06 §2.4。"""

    RAW_EVENT = "raw_event"
    ENTITY = "entity"
    FACT = "fact"


@dataclass(frozen=True)
class Hit:
    ref_type: RefType
    ref_id: str
    distance: float
    """余弦距离,**越小越像**。0 是完全一致,1 是正交。

    刻意不翻译成"相似度":翻译一次就要在两处保持一致,而 pgvector 的
    `<=>` 返回的就是距离。
    """


_VECTOR_PARAM = bindparam("embedding", type_=Vector())
"""向量列的绑定类型,来自 `pgvector` 的 SQLAlchemy 适配。

ADR-017 装这个包就是为了这一行 —— 自己拼 `[0.1,0.2,...]` 字面量也能跑,
但那等于把一份格式约定散在每个写向量的地方。
"""

_UPSERT = text("""
    INSERT INTO embeddings (user_id, ref_type, ref_id, embedding, model)
    VALUES (:user_id, :ref_type, :ref_id, :embedding, :model)
    ON CONFLICT (user_id, ref_type, ref_id, model)
    DO UPDATE SET embedding = EXCLUDED.embedding, created_at = now()
""").bindparams(_VECTOR_PARAM)

_SEARCH = text("""
    SELECT ref_type, ref_id, embedding <=> :query AS distance
      FROM embeddings
     WHERE user_id = :user_id
       AND model = :model
       AND ref_type = :ref_type
     ORDER BY embedding <=> :query
     LIMIT :limit
""").bindparams(bindparam("query", type_=Vector()))

_MISSING_FACTS = text("""
    SELECT f.id, f.statement
      FROM facts f
     WHERE f.user_id = :user_id
       AND f.negated_by_user = false
       AND NOT EXISTS (
           SELECT 1
             FROM embeddings e
            WHERE e.user_id = f.user_id
              AND e.ref_type = 'fact'
              AND e.ref_id = f.id::text
              AND e.model = :model
       )
     ORDER BY f.created_at
     LIMIT :limit
""")

_COUNT_STALE = text("""
    SELECT count(*) FROM embeddings WHERE user_id = :user_id AND model <> :model
""")


def upsert(
    user_id: str,
    session: Session,
    *,
    ref_type: RefType,
    ref_id: str,
    embedding: Sequence[float],
    model: str,
) -> None:
    """写入或覆盖一条向量。

    覆盖而不是跳过:事实的 provenance 会被合并、statement 也可能被更正过,
    重算一次是对的。真正不该重复的是"每天把没变的东西再算一遍",
    那个由 `facts_missing_embeddings` 挡住 —— 它只挑还没有向量的。
    """
    session.execute(
        _UPSERT,
        {
            "user_id": user_id,
            "ref_type": ref_type.value,
            "ref_id": ref_id,
            "embedding": list(embedding),
            "model": model,
        },
    )


def search(
    user_id: str,
    session: Session,
    *,
    query_embedding: Sequence[float],
    model: str,
    ref_type: RefType = RefType.FACT,
    limit: int = 10,
) -> list[Hit]:
    """按向量找最像的若干条。**只在同一个 model 内比。**"""
    rows = session.execute(
        _SEARCH,
        {
            "user_id": user_id,
            "query": list(query_embedding),
            "model": model,
            "ref_type": ref_type.value,
            "limit": limit,
        },
    ).all()
    return [
        Hit(ref_type=RefType(row.ref_type), ref_id=row.ref_id, distance=float(row.distance))
        for row in rows
    ]


@dataclass(frozen=True)
class PendingFact:
    fact_id: str
    statement: str


def facts_missing_embeddings(
    user_id: str, session: Session, *, model: str, limit: int = 100
) -> list[PendingFact]:
    """哪些事实还没有当前模型的向量。

    按"缺什么补什么"来,而不是"这次抽出来的补一下":抽取失败过、
    embedding 接口挂过、换过模型 —— 三种情况都会留下没有向量的事实,
    而它们的表现完全一样(那条事实召不回来)。用一个查询覆盖三种,
    比在每条失败路径上记账可靠。
    """
    rows = session.execute(
        _MISSING_FACTS, {"user_id": user_id, "model": model, "limit": limit}
    ).all()
    return [PendingFact(fact_id=str(row.id), statement=row.statement) for row in rows]


def count_stale(user_id: str, session: Session, *, model: str) -> int:
    """有多少条向量是别的模型算的。

    换模型之后这个数就是"还有多少要重算"。它不会自己变小 ——
    旧向量不删,是为了换模型出问题时还退得回去。
    """
    return int(session.execute(_COUNT_STALE, {"user_id": user_id, "model": model}).scalar_one())
