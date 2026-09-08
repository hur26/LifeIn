"""事实库的集成测试。需要真实 PostgreSQL(见 conftest.py)。

这一组里有一个用例是**绕过仓储直接写 SQL** 的
(`test_database_rejects_empty_provenance`)—— 它测的不是仓储,是迁移里那条
`cardinality(provenance) >= 1` 的 CHECK 到底建出来没有。06 §2.3 记着那个坑:
写成 `array_length` 的话空数组返回 NULL,CHECK 判定为通过,约束形同虚设,
而这个错**只有连真库才发现得了**。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from lifein.models.normalized import Trust
from lifein.repos.facts import (
    EXTERNAL_MAX_CONFIDENCE,
    FactError,
    add_fact,
    confirm_fact,
    expire_fact,
    get_fact,
    list_active_facts,
    negate_fact,
    search_facts,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
AGENT = "memory"


def add(session, user_id: str, statement: str, **overrides):
    params = dict(
        statement=statement,
        provenance=[1],
        confidence=0.5,
        trust=Trust.EXTERNAL,
        created_by_agent=AGENT,
        valid_from=NOW,
    )
    params.update(overrides)
    return add_fact(user_id, session, **params)


def test_insert_and_read_back(pg_session, user_id):
    result = add(pg_session, user_id, "张三在市场部")
    assert result.created is True

    stored = get_fact(user_id, pg_session, fact_id=result.fact.id)
    assert stored.statement == "张三在市场部"
    assert stored.provenance == [1]
    assert stored.created_by_agent == AGENT


def test_external_confidence_is_capped_on_write(pg_session, user_id):
    result = add(pg_session, user_id, "张三下周离职", confidence=0.95, trust=Trust.EXTERNAL)
    assert result.fact.confidence == pytest.approx(EXTERNAL_MAX_CONFIDENCE)


def test_repo_rejects_fact_without_provenance(pg_session, user_id):
    # 铁律 5。仓储这一道给的是人话,库那一道是兜底
    with pytest.raises(FactError):
        add(pg_session, user_id, "凭空来的一条", provenance=[])


def test_database_rejects_empty_provenance(pg_session, user_id):
    """绕过仓储直接写,验证 CHECK 本身。

    仓储那一道拦得住是它自己的事;这条测的是"仓储被绕过时还有没有防线"。
    """
    with pytest.raises(IntegrityError):
        pg_session.execute(
            text("""
                INSERT INTO facts
                    (user_id, statement, provenance, confidence, valid_from, created_by_agent)
                VALUES (:u, '绕过仓储写的', '{}', 0.5, :t, 'manual')
            """),
            {"u": user_id, "t": NOW},
        )


def test_same_statement_merges_provenance_instead_of_duplicating(pg_session, user_id):
    """每天重跑抽取时,同一句话会被抽出无数遍。

    重复入库的后果不是浪费空间,是 prompt 里同一件事被说十遍,
    挤掉别的事实 —— 摘要会因此变差,而原因非常难查。
    """
    first = add(pg_session, user_id, "张三在市场部", provenance=[1])
    again = add(pg_session, user_id, "张三  在市场部 ", provenance=[7])

    assert again.created is False
    assert again.reason == "merged"
    assert again.fact.id == first.fact.id
    assert again.fact.provenance == [1, 7]


def test_repeated_evidence_does_not_raise_external_confidence(pg_session, user_id):
    add(pg_session, user_id, "张三在市场部", provenance=[1], confidence=0.6)
    again = add(pg_session, user_id, "张三在市场部", provenance=[2], confidence=0.95)
    # 同一件事被外部说了两遍,它还是外部说的
    assert again.fact.confidence == pytest.approx(EXTERNAL_MAX_CONFIDENCE)


def test_negated_fact_is_never_reinferred(pg_session, user_id):
    """用户否定过的事实不许再被写回来。

    抽取每天跑一次,那封邮件明天还在。第二次冒出来比第一次更伤 ——
    用户会得出"否定这个动作没有用"的结论,然后再也不用它。
    """
    created = add(pg_session, user_id, "张三下周离职")
    assert negate_fact(user_id, pg_session, fact_id=created.fact.id) is True

    retry = add(pg_session, user_id, "张三下周离职", provenance=[9])
    assert retry.fact is None
    assert retry.reason == "negated"

    # 记录保留不删 —— 它正是"不要再推断这条"的依据
    stored = get_fact(user_id, pg_session, fact_id=created.fact.id)
    assert stored.negated_by_user is True
    assert stored.provenance == [1], "被否定的事实不该再被合并进新证据"


def test_confirmation_is_the_only_way_past_the_cap(pg_session, user_id):
    created = add(pg_session, user_id, "张三在市场部", confidence=0.95, trust=Trust.EXTERNAL)
    assert created.fact.confidence == pytest.approx(EXTERNAL_MAX_CONFIDENCE)

    assert confirm_fact(user_id, pg_session, fact_id=created.fact.id) is True
    stored = get_fact(user_id, pg_session, fact_id=created.fact.id)
    assert stored.confirmed_by_user is True
    assert stored.confidence == pytest.approx(1.0)


def test_confirmed_confidence_survives_later_extraction(pg_session, user_id):
    created = add(pg_session, user_id, "张三在市场部")
    confirm_fact(user_id, pg_session, fact_id=created.fact.id)

    again = add(pg_session, user_id, "张三在市场部", provenance=[3], confidence=0.4)
    # 用户点过的东西不许被后来的推断值压下去
    assert again.fact.confidence == pytest.approx(1.0)
    assert again.fact.provenance == [1, 3]


def test_confirming_a_negated_fact_clears_the_negation(pg_session, user_id):
    # 先否定后确认,说明当初是否错了。留着标记会让这条永远回不来
    created = add(pg_session, user_id, "张三在市场部")
    negate_fact(user_id, pg_session, fact_id=created.fact.id)
    confirm_fact(user_id, pg_session, fact_id=created.fact.id)

    stored = get_fact(user_id, pg_session, fact_id=created.fact.id)
    assert stored.negated_by_user is False


def test_expired_fact_drops_out_of_the_active_list(pg_session, user_id):
    """失效不是否定:它曾经是对的。

    分开是为了 App 上那份"你否定过什么"的清单还有意义 —— 那份清单是
    抽取该学会不做什么的唯一依据,掺进过期事实就没法看了。
    """
    created = add(pg_session, user_id, "张三在深圳出差")
    assert expire_fact(
        user_id, pg_session, fact_id=created.fact.id, valid_until=NOW + timedelta(days=1)
    )

    assert list_active_facts(user_id, pg_session, at=NOW)
    assert list_active_facts(user_id, pg_session, at=NOW + timedelta(days=2)) == []
    assert get_fact(user_id, pg_session, fact_id=created.fact.id).negated_by_user is False


def test_active_list_hides_negated_and_low_confidence(pg_session, user_id):
    keep = add(pg_session, user_id, "张三在市场部", confidence=0.6)
    weak = add(pg_session, user_id, "张三可能养了猫", confidence=0.2)
    dropped = add(pg_session, user_id, "张三下周离职")
    negate_fact(user_id, pg_session, fact_id=dropped.fact.id)

    statements = [f.statement for f in list_active_facts(user_id, pg_session, at=NOW)]
    assert keep.fact.statement in statements
    assert weak.fact.statement in statements
    assert dropped.fact.statement not in statements

    filtered = list_active_facts(user_id, pg_session, at=NOW, min_confidence=0.5)
    assert [f.statement for f in filtered] == [keep.fact.statement]


def test_confirmed_facts_come_first(pg_session, user_id):
    # 进 prompt 的条数有限,该先给最靠得住的
    add(pg_session, user_id, "张三在市场部", confidence=0.6)
    confirmed = add(pg_session, user_id, "张三是我同事", confidence=0.3)
    confirm_fact(user_id, pg_session, fact_id=confirmed.fact.id, confidence=0.65)

    facts = list_active_facts(user_id, pg_session, at=NOW)
    assert facts[0].statement == "张三是我同事"


def test_search_skips_negated(pg_session, user_id):
    add(pg_session, user_id, "张三在市场部")
    gone = add(pg_session, user_id, "张三下周离职")
    negate_fact(user_id, pg_session, fact_id=gone.fact.id)

    hits = [f.statement for f in search_facts(user_id, pg_session, query="张三")]
    assert hits == ["张三在市场部"]
    assert search_facts(user_id, pg_session, query="   ") == []


def test_facts_are_isolated_per_user(pg_session, user_id):
    """铁律 1 的实测:别的用户的记忆一条都看不见。"""
    import uuid

    other = str(uuid.uuid4())
    pg_session.execute(
        text("INSERT INTO users (id, display_name, wecom_userid) VALUES (:id, :n, :w)"),
        {"id": other, "n": "另一个人", "w": f"other-{other[:8]}"},
    )
    mine = add(pg_session, user_id, "张三在市场部")

    assert list_active_facts(other, pg_session, at=NOW) == []
    assert search_facts(other, pg_session, query="张三") == []
    assert get_fact(other, pg_session, fact_id=mine.fact.id) is None
    assert negate_fact(other, pg_session, fact_id=mine.fact.id) is False
