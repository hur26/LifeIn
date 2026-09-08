"""向量索引的集成测试。需要真实 PostgreSQL + pgvector(见 conftest.py)。

**这一组只能连真库测。** 余弦距离、HNSW 索引、`vector` 类型的绑定,
没有一样是 SQLite 或者假对象能替代的 —— 用它们测出来的"通过"是假的。

维度必须是 1024:建表时写死的 `VECTOR(1024)`。测试里用稀疏的单位向量,
既好读又能让距离的期望值一眼可算。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from lifein.models.normalized import Trust
from lifein.repos import facts
from lifein.repos.embeddings import (
    RefType,
    count_stale,
    facts_missing_embeddings,
    search,
    upsert,
)

pytestmark = pytest.mark.integration

DIM = 1024
MODEL = "emb-v1"
NOW = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)


def vec(*positions: int) -> list[float]:
    """在指定位置上置 1 的稀疏向量。位置相同 = 完全一样。"""
    v = [0.0] * DIM
    for p in positions:
        v[p] = 1.0
    return v


def add_fact(session, user_id: str, statement: str):
    return facts.add_fact(
        user_id,
        session,
        statement=statement,
        provenance=[1],
        confidence=0.5,
        trust=Trust.EXTERNAL,
        created_by_agent="memory",
        valid_from=NOW - timedelta(days=1),
    ).fact


def test_upsert_then_find_the_nearest(pg_session, user_id):
    near = add_fact(pg_session, user_id, "张三负责报销审批")
    far = add_fact(pg_session, user_id, "周三下午有牙医预约")
    upsert(
        user_id, pg_session, ref_type=RefType.FACT, ref_id=near.id, embedding=vec(0), model=MODEL
    )
    upsert(user_id, pg_session, ref_type=RefType.FACT, ref_id=far.id, embedding=vec(5), model=MODEL)

    hits = search(user_id, pg_session, query_embedding=vec(0), model=MODEL, limit=2)

    assert [h.ref_id for h in hits] == [near.id, far.id]
    assert hits[0].distance == pytest.approx(0.0, abs=1e-6)  # 一样的向量距离是 0
    assert hits[1].distance > hits[0].distance


def test_upsert_overwrites_instead_of_duplicating(pg_session, user_id):
    """同一条事实只该有一条当前模型的向量。

    事实的 statement 会被更正、provenance 会被合并,重算是对的;
    但留下两条向量会让同一条事实在召回里出现两次,挤掉别的。
    """
    fact = add_fact(pg_session, user_id, "张三负责报销审批")
    upsert(
        user_id, pg_session, ref_type=RefType.FACT, ref_id=fact.id, embedding=vec(0), model=MODEL
    )
    upsert(
        user_id, pg_session, ref_type=RefType.FACT, ref_id=fact.id, embedding=vec(7), model=MODEL
    )

    count = pg_session.execute(
        text("SELECT count(*) FROM embeddings WHERE user_id = :u AND ref_id = :r"),
        {"u": user_id, "r": fact.id},
    ).scalar_one()
    assert count == 1

    # 留下的是新的那条
    hits = search(user_id, pg_session, query_embedding=vec(7), model=MODEL, limit=1)
    assert hits[0].distance == pytest.approx(0.0, abs=1e-6)


def test_other_models_vectors_are_never_compared(pg_session, user_id):
    """两个模型的向量之间比距离,得到的数看着正常、实际无意义(06 §2.4)。

    这种错不会报警,只会让召回悄悄变差 —— 所以查询永远带 model 条件。
    """
    old = add_fact(pg_session, user_id, "用旧模型算过的事实")
    upsert(
        user_id, pg_session, ref_type=RefType.FACT, ref_id=old.id, embedding=vec(0), model="emb-v0"
    )

    assert search(user_id, pg_session, query_embedding=vec(0), model=MODEL) == []
    assert count_stale(user_id, pg_session, model=MODEL) == 1


def test_missing_list_covers_every_way_a_vector_can_be_absent(pg_session, user_id):
    """抽取失败过、embedding 接口挂过、换过模型 —— 表现完全一样。

    所以问的是"谁还没有向量",不是"这次抽了谁"。
    """
    done = add_fact(pg_session, user_id, "已经算过向量的事实")
    todo = add_fact(pg_session, user_id, "还没算过的事实")
    upsert(
        user_id, pg_session, ref_type=RefType.FACT, ref_id=done.id, embedding=vec(1), model=MODEL
    )

    pending = facts_missing_embeddings(user_id, pg_session, model=MODEL)
    assert [p.fact_id for p in pending] == [todo.id]
    assert pending[0].statement == "还没算过的事实"


def test_negated_facts_are_not_queued_for_embedding(pg_session, user_id):
    # 用户否定过的事实不该再花钱算向量,也不该被召回
    gone = add_fact(pg_session, user_id, "一条被否定的事实")
    facts.negate_fact(user_id, pg_session, fact_id=gone.id)

    assert facts_missing_embeddings(user_id, pg_session, model=MODEL) == []


def test_vectors_are_isolated_per_user(pg_session, user_id):
    other = str(uuid.uuid4())
    pg_session.execute(
        text("INSERT INTO users (id, display_name, wecom_userid) VALUES (:id, :n, :w)"),
        {"id": other, "n": "另一个人", "w": f"other-{other[:8]}"},
    )
    mine = add_fact(pg_session, user_id, "我的事实")
    upsert(
        user_id, pg_session, ref_type=RefType.FACT, ref_id=mine.id, embedding=vec(0), model=MODEL
    )

    assert search(other, pg_session, query_embedding=vec(0), model=MODEL) == []
    assert count_stale(other, pg_session, model=MODEL) == 0
