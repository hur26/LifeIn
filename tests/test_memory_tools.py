"""记忆检索工具的集成测试。需要真实 PostgreSQL(见 conftest.py)。

**全部经网关调用,不直接调函数。** 那三个工具存在的理由就是"这次读取要留在
`tool_calls` 里",绕过网关去测等于没测到那件事。

顺带说明一个测试上的坑:别的测试文件会 `clear_registry()` 做隔离,而模块级的
`@tool` 只在第一次 import 时执行。整套跑起来顺序一变,这里就会拿到一张空的
注册表 —— 所以下面那个夹具会在必要时重新 import 一次。
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel
from sqlalchemy import text

import lifein.tools.memory as memory_tools
from lifein.agents import contract as agent_contract
from lifein.agents.contract import OnUncertain, agent
from lifein.governance import registry
from lifein.governance.audit import InMemoryAuditSink
from lifein.governance.gateway import CallContext, Denied, Gateway
from lifein.models.normalized import (
    EventKind,
    ExternalRef,
    IdentifierType,
    NormalizedEvent,
    Party,
    PartyRole,
    Trust,
)
from lifein.repos import embeddings, facts, raw_events
from lifein.repos.entities import AliasType, EntityKind, resolve_or_create
from lifein.sources.base import IngestedEvent

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
AGENT = "test_recall"
ALL_TOOLS = ["memory.search_entities", "memory.recent_events_with", "memory.recall_facts"]


class _Args(BaseModel):
    pass


class _Out(BaseModel):
    pass


@pytest.fixture(autouse=True)
def _registered():
    if "memory.recall_facts" not in registry.registered_tools():
        importlib.reload(memory_tools)
    if AGENT not in agent_contract.registered_agents():

        @agent(
            name=AGENT,
            inputs=_Args,
            tools=ALL_TOOLS,
            output_schema=_Out,
            on_uncertain=OnUncertain.DEGRADE,
            evalset="evals/test.jsonl",
        )
        def _handler(_: _Args) -> _Out: ...

    yield


@pytest.fixture
def gateway() -> Gateway:
    return Gateway(InMemoryAuditSink())


def ctx(session, user_id: str, agent_name: str = AGENT) -> CallContext:
    return CallContext(
        user_id=user_id, agent=agent_name, trust=Trust.USER_INPUT, session=session
    )


def ingest(session, user_id: str, external_id: str, *, sender: str, email: str, at=NOW):
    raw_events.insert_events(
        user_id,
        session,
        [
            IngestedEvent(
                source="email",
                external_id=external_id,
                occurred_at=at,
                trust=Trust.EXTERNAL,
                raw={"subject": "报销单"},
                normalized=NormalizedEvent(
                    kind=EventKind.MESSAGE,
                    title=f"来自 {sender} 的邮件",
                    occurred_at=at,
                    external_ref=ExternalRef(source="email", external_id=external_id),
                    trust=Trust.EXTERNAL,
                    confidence=1.0,
                    body="周三下午三点开个会。",
                    parties=[
                        Party(
                            role=PartyRole.FROM,
                            display_name=sender,
                            identifier=email,
                            identifier_type=IdentifierType.EMAIL,
                        )
                    ],
                ),
            )
        ],
    )


def test_search_entities_returns_matches(pg_session, user_id, gateway):
    resolve_or_create(
        user_id,
        pg_session,
        kind=EntityKind.PERSON,
        name="张三",
        seen_at=NOW,
        identifier="zhang@qq.com",
        identifier_type=AliasType.EMAIL,
        evidence_event_id=1,
    )

    result = gateway.call(
        ctx(pg_session, user_id), "memory.search_entities", {"name": "张"}
    )
    assert [r["name"] for r in result] == ["张三"]


def test_recent_events_uses_every_alias_of_the_person(pg_session, user_id, gateway):
    """他换过邮箱、署名也变过,问"老张"照样找得到那些信。

    这正是"过一遍实体"而不是"拿关键词搜正文"的意义。
    """
    ingest(pg_session, user_id, "m1", sender="张伟", email="zhang@qq.com")
    ingest(
        pg_session,
        user_id,
        "m2",
        sender="老张",
        email="zhang@work.com",
        at=NOW + timedelta(hours=1),
    )

    entity = resolve_or_create(
        user_id,
        pg_session,
        kind=EntityKind.PERSON,
        name="张伟",
        seen_at=NOW,
        identifier="zhang@qq.com",
        identifier_type=AliasType.EMAIL,
        evidence_event_id=1,
    ).entity
    # 第二个邮箱与第二个署名都挂到同一个人身上
    resolve_or_create(
        user_id,
        pg_session,
        kind=EntityKind.PERSON,
        name="老张",
        seen_at=NOW,
        evidence_event_id=2,
    )
    from lifein.repos.entities import link_alias

    link_alias(
        user_id,
        pg_session,
        entity_id=entity.id,
        alias="zhang@work.com",
        alias_type=AliasType.EMAIL,
        evidence_event_id=2,
    )

    result = gateway.call(
        ctx(pg_session, user_id), "memory.recent_events_with", {"name": "张伟"}
    )
    assert {e["external_id"] for e in result["events"]} == {"m1", "m2"}
    # 最近的在前:问"上次"要的是最后一次
    assert result["events"][0]["external_id"] == "m2"


def test_recent_events_reports_other_candidates_instead_of_guessing(pg_session, user_id, gateway):
    """重名不替用户选。

    猜一个然后自信地答错,比说"你是指哪个李工"糟得多。
    """
    for suffix in ("a", "b"):
        ingest(pg_session, user_id, f"m-{suffix}", sender="李工", email=f"li.{suffix}@corp.com")
        resolve_or_create(
            user_id,
            pg_session,
            kind=EntityKind.PERSON,
            name="李工",
            seen_at=NOW,
            identifier=f"li.{suffix}@corp.com",
            identifier_type=AliasType.EMAIL,
            evidence_event_id=1,
        )

    result = gateway.call(
        ctx(pg_session, user_id), "memory.recent_events_with", {"name": "李工"}
    )
    assert result["entity"] is not None
    assert result["other_matches"] == ["李工"], "另一个同名的人要原样报上来"


def test_recent_events_with_unknown_person_returns_empty(pg_session, user_id, gateway):
    result = gateway.call(
        ctx(pg_session, user_id), "memory.recent_events_with", {"name": "查无此人"}
    )
    assert result == {"entity": None, "events": [], "other_matches": []}


def test_recall_facts_carries_provenance_and_confidence(pg_session, user_id, gateway):
    """置信度和来源必须跟着事实一起回。

    不带的话,一条 0.4 分、来自群发邮件的推断,在 prompt 里看起来和用户
    亲口确认过的一模一样 —— 记忆污染就是这么变得无法追查的。
    """
    facts.add_fact(
        user_id,
        pg_session,
        statement="张三在市场部",
        provenance=[7],
        confidence=0.5,
        trust=Trust.EXTERNAL,
        created_by_agent="memory",
        valid_from=NOW - timedelta(days=1),
    )

    [recalled] = gateway.call(ctx(pg_session, user_id), "memory.recall_facts", {"query": "张三"})
    assert recalled["statement"] == "张三在市场部"
    assert recalled["provenance"] == [7]
    assert recalled["confidence"] == pytest.approx(0.5)
    assert recalled["confirmed_by_user"] is False


def test_recall_facts_without_query_lists_the_active_ones(pg_session, user_id, gateway):
    facts.add_fact(
        user_id,
        pg_session,
        statement="我答应张三周三交报销",
        provenance=[1],
        confidence=0.5,
        trust=Trust.EXTERNAL,
        created_by_agent="memory",
        valid_from=NOW - timedelta(days=1),
    )
    result = gateway.call(ctx(pg_session, user_id), "memory.recall_facts", {})
    assert len(result) == 1


def test_tool_calls_are_audited(pg_session, user_id):
    """这三个工具存在的理由:读取要留下痕迹。"""
    audit = InMemoryAuditSink()
    Gateway(audit).call(ctx(pg_session, user_id), "memory.search_entities", {"name": "张"})

    entry = audit.entries[-1]
    assert entry.tool_name == "memory.search_entities"
    assert entry.level.value == "L1"
    assert entry.result_status == "allowed"
    assert entry.agent == AGENT


def test_agent_without_whitelist_is_denied(pg_session, user_id, gateway):
    """L1 也不是谁都能调 —— (agent, tool) 双重门。"""
    if "no_tools" not in agent_contract.registered_agents():

        @agent(
            name="no_tools",
            inputs=_Args,
            tools=[],
            output_schema=_Out,
            on_uncertain=OnUncertain.DO_NOTHING,
            evalset="evals/test.jsonl",
        )
        def _handler(_: _Args) -> _Out: ...

    with pytest.raises(Denied):
        gateway.call(
            ctx(pg_session, user_id, "no_tools"), "memory.search_entities", {"name": "张"}
        )


def test_tools_refuse_to_open_their_own_transaction(pg_session, user_id, gateway):
    """没给 session 就当场炸,不许自己连一个。

    退化成"自己开一个事务"会让 06 §2.7 那条同事务约束在 L2 上线时悄悄失效,
    而那种失效的表现是"确认了但没写进去",极难查。
    """
    with pytest.raises(RuntimeError):
        gateway.call(
            CallContext(user_id=user_id, agent=AGENT, trust=Trust.USER_INPUT),
            "memory.search_entities",
            {"name": "张"},
        )


def test_memory_is_isolated_per_user(pg_session, user_id, gateway):
    import uuid

    other = str(uuid.uuid4())
    pg_session.execute(
        text("INSERT INTO users (id, display_name, wecom_userid) VALUES (:id, :n, :w)"),
        {"id": other, "n": "另一个人", "w": f"other-{other[:8]}"},
    )
    ingest(pg_session, user_id, "m1", sender="张三", email="zhang@qq.com")
    resolve_or_create(
        user_id,
        pg_session,
        kind=EntityKind.PERSON,
        name="张三",
        seen_at=NOW,
        identifier="zhang@qq.com",
        identifier_type=AliasType.EMAIL,
        evidence_event_id=1,
    )

    result = gateway.call(ctx(pg_session, other), "memory.recent_events_with", {"name": "张三"})
    assert result["entity"] is None


# ---------- 向量召回(第 5 片) ----------

DIM = 1024
EMB_MODEL = "emb-v1"


def vec(position: int) -> list[float]:
    v = [0.0] * DIM
    v[position] = 1.0
    return v


def seed_fact(pg_session, user_id: str, statement: str, *, position: int | None = None):
    created = facts.add_fact(
        user_id,
        pg_session,
        statement=statement,
        provenance=[1],
        confidence=0.5,
        trust=Trust.EXTERNAL,
        created_by_agent="memory",
        valid_from=NOW - timedelta(days=1),
    ).fact
    if position is not None:
        embeddings.upsert(
            user_id,
            pg_session,
            ref_type=embeddings.RefType.FACT,
            ref_id=created.id,
            embedding=vec(position),
            model=EMB_MODEL,
        )
    return created


def test_vector_recall_finds_what_the_words_miss(pg_session, user_id, gateway):
    """"上次那个报销的事" —— 用户的说法和事实的措辞对不上。

    字面检索在这里必然落空,向量补的就是这一段(ADR-019)。
    """
    seed_fact(pg_session, user_id, "张三负责差旅费用审批", position=0)

    result = gateway.call(
        ctx(pg_session, user_id),
        "memory.recall_facts",
        {"query": "报销", "query_embedding": vec(0), "embedding_model": EMB_MODEL},
    )
    assert [r["statement"] for r in result] == ["张三负责差旅费用审批"]


def test_vector_hits_come_before_literal_ones(pg_session, user_id, gateway):
    # 超出 limit 被切掉时,该先保住"意思像的"而不是"字面撞上的"
    seed_fact(pg_session, user_id, "张三负责差旅费用审批", position=0)
    seed_fact(pg_session, user_id, "报销单要贴发票")

    result = gateway.call(
        ctx(pg_session, user_id),
        "memory.recall_facts",
        {"query": "报销", "query_embedding": vec(0), "embedding_model": EMB_MODEL},
    )
    assert [r["statement"] for r in result] == ["张三负责差旅费用审批", "报销单要贴发票"]


def test_the_same_fact_is_not_returned_twice(pg_session, user_id, gateway):
    # 向量和字面都命中同一条时,重复送进 prompt 会让模型觉得它更重要
    seed_fact(pg_session, user_id, "报销单要贴发票", position=0)

    result = gateway.call(
        pg_session and ctx(pg_session, user_id),
        "memory.recall_facts",
        {"query": "报销", "query_embedding": vec(0), "embedding_model": EMB_MODEL},
    )
    assert len(result) == 1


def test_negated_fact_is_not_recalled_even_with_a_vector(pg_session, user_id, gateway):
    """向量不随否定动作删,所以过滤只有一个地方靠得住:取正文那一步。"""
    gone = seed_fact(pg_session, user_id, "张三下周离职", position=0)
    facts.negate_fact(user_id, pg_session, fact_id=gone.id)

    result = gateway.call(
        ctx(pg_session, user_id),
        "memory.recall_facts",
        {"query": "离职", "query_embedding": vec(0), "embedding_model": EMB_MODEL},
    )
    assert result == []


def test_without_a_vector_it_falls_back_to_literal_search(pg_session, user_id, gateway):
    # 没配 EMBEDDING_MODEL 的部署走的就是这条路
    seed_fact(pg_session, user_id, "报销单要贴发票")

    result = gateway.call(ctx(pg_session, user_id), "memory.recall_facts", {"query": "报销"})
    assert [r["statement"] for r in result] == ["报销单要贴发票"]
