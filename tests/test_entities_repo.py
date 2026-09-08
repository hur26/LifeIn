"""实体库的集成测试。需要真实 PostgreSQL(见 conftest.py)。

重点全在**归并的边界**上,而不是"能不能写进去":

- 同一个人换了署名 → 还是一个实体
- 两个人同名 → 靠精确标识符分开,而且冲突时**一行都不改**
- 同一条事件重跑 → 置信度不许因此上涨
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from lifein.repos.entities import (
    CONFIRMED,
    INITIAL_NAME,
    INITIAL_STRONG,
    MAX_EVIDENCE,
    AliasError,
    AliasType,
    EntityKind,
    confirm_alias,
    find_by_alias,
    get_entity,
    link_alias,
    resolve_or_create,
    search_entities,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)


def alias_row(session, user_id: str, alias: str, alias_type: AliasType):
    return session.execute(
        text("""
            SELECT entity_id, confidence, evidence_event_ids
              FROM entity_aliases
             WHERE user_id = :u AND alias = :a AND alias_type = :t
        """),
        {"u": user_id, "a": alias, "t": alias_type.value},
    ).one()


def test_creates_entity_with_both_aliases(pg_session, user_id):
    result = resolve_or_create(
        user_id,
        pg_session,
        kind=EntityKind.PERSON,
        name="张三",
        seen_at=NOW,
        identifier="Zhang@QQ.com",
        identifier_type=AliasType.EMAIL,
        evidence_event_id=1,
    )

    assert result.created is True
    assert result.conflicted is False
    assert result.entity.canonical_name == "张三"

    # 邮箱按归一化后的键存,不存原样 —— 否则大小写不同就是两个实体
    by_email = find_by_alias(user_id, pg_session, alias="zhang@qq.com", alias_type=AliasType.EMAIL)
    by_name = find_by_alias(user_id, pg_session, alias="张三", alias_type=AliasType.NAME)
    assert by_email is not None
    assert by_email.id == by_name.id == result.entity.id


def test_same_person_new_display_name_is_one_entity(pg_session, user_id):
    """署名换了,邮箱没换 —— 还是同一个人。

    这正是"先标识符后名字"的意义:反过来会凭空多出一个实体。
    """
    first = resolve_or_create(
        user_id,
        pg_session,
        kind=EntityKind.PERSON,
        name="张三",
        seen_at=NOW,
        identifier="zhang@qq.com",
        identifier_type=AliasType.EMAIL,
        evidence_event_id=1,
    )
    second = resolve_or_create(
        user_id,
        pg_session,
        kind=EntityKind.PERSON,
        name="张三(市场部)",
        seen_at=NOW + timedelta(days=1),
        identifier="ZHANG@qq.com",
        identifier_type=AliasType.EMAIL,
        evidence_event_id=2,
    )

    assert second.created is False
    assert second.entity.id == first.entity.id
    # 新署名作为别名也挂上去了,下次只写名字也认得出
    assert (
        find_by_alias(user_id, pg_session, alias="张三(市场部)", alias_type=AliasType.NAME).id
        == first.entity.id
    )


def test_last_seen_moves_forward_first_seen_does_not(pg_session, user_id):
    resolve_or_create(
        user_id, pg_session, kind=EntityKind.PERSON, name="张三", seen_at=NOW, evidence_event_id=1
    )
    # 补历史邮件时会拿到更早的时间,那时 first_seen 该往前走,last_seen 不许往回缩
    older = resolve_or_create(
        user_id,
        pg_session,
        kind=EntityKind.PERSON,
        name="张三",
        seen_at=NOW - timedelta(days=30),
        evidence_event_id=2,
    )
    assert older.entity.first_seen_at == NOW - timedelta(days=30)
    assert older.entity.last_seen_at == NOW


def test_name_collision_split_by_identifier(pg_session, user_id):
    """两个"李工",邮箱不同 —— 必须是两个实体。

    名字撞了不算证据,这就是 name 起步只有 0.5 的原因。
    """
    a = resolve_or_create(
        user_id,
        pg_session,
        kind=EntityKind.PERSON,
        name="李工",
        seen_at=NOW,
        identifier="li.a@corp.com",
        identifier_type=AliasType.EMAIL,
        evidence_event_id=1,
    )
    b = resolve_or_create(
        user_id,
        pg_session,
        kind=EntityKind.PERSON,
        name="李工",
        seen_at=NOW,
        identifier="li.b@corp.com",
        identifier_type=AliasType.EMAIL,
        evidence_event_id=2,
    )

    assert b.entity.id != a.entity.id
    assert b.conflicted is True, "名字已指向另一个实体,这是冲突"
    # 冲突时不动作(铁律 7):名字那条别名还指着第一个人,一个字节没改
    assert str(alias_row(pg_session, user_id, "李工", AliasType.NAME).entity_id) == a.entity.id
    still = find_by_alias(user_id, pg_session, alias="李工", alias_type=AliasType.NAME)
    assert still.id == a.entity.id


def test_replayed_event_does_not_inflate_confidence(pg_session, user_id):
    """同一条事件重跑不许让置信度上涨。

    归一化重跑是常规操作(06 §1.4),证据按 raw_events.id 去重就是为它准备的。
    """
    for _ in range(5):
        resolve_or_create(
            user_id,
            pg_session,
            kind=EntityKind.PERSON,
            name="王五",
            seen_at=NOW,
            evidence_event_id=42,
        )

    row = alias_row(pg_session, user_id, "王五", AliasType.NAME)
    assert list(row.evidence_event_ids) == [42]
    assert float(row.confidence) == pytest.approx(INITIAL_NAME)


def test_distinct_events_climb_to_cap(pg_session, user_id):
    entity_id = resolve_or_create(
        user_id, pg_session, kind=EntityKind.PERSON, name="王五", seen_at=NOW, evidence_event_id=1
    ).entity.id

    for event_id in range(2, 12):
        link_alias(
            user_id,
            pg_session,
            entity_id=entity_id,
            alias="王五",
            alias_type=AliasType.NAME,
            evidence_event_id=event_id,
        )

    row = alias_row(pg_session, user_id, "王五", AliasType.NAME)
    # 封顶 0.9:系统再有把握也到不了 1.0
    assert float(row.confidence) == pytest.approx(0.9)


def test_evidence_list_is_capped_and_keeps_the_earliest(pg_session, user_id):
    entity_id = resolve_or_create(
        user_id, pg_session, kind=EntityKind.PERSON, name="王五", seen_at=NOW, evidence_event_id=1
    ).entity.id

    for event_id in range(2, MAX_EVIDENCE + 10):
        link_alias(
            user_id,
            pg_session,
            entity_id=entity_id,
            alias="王五",
            alias_type=AliasType.NAME,
            evidence_event_id=event_id,
        )

    evidence = list(alias_row(pg_session, user_id, "王五", AliasType.NAME).evidence_event_ids)
    assert len(evidence) == MAX_EVIDENCE
    # 留最早的:这一列要回答的是"当初凭什么归并",不是"最近又见过几次"
    assert evidence[0] == 1
    assert evidence[-1] == MAX_EVIDENCE


def test_email_alias_starts_high(pg_session, user_id):
    resolve_or_create(
        user_id,
        pg_session,
        kind=EntityKind.MERCHANT,
        name="某某超市",
        seen_at=NOW,
        identifier="MCC5411",
        identifier_type=AliasType.MERCHANT_CODE,
        evidence_event_id=1,
    )
    row = alias_row(pg_session, user_id, "mcc5411", AliasType.MERCHANT_CODE)
    assert float(row.confidence) == pytest.approx(INITIAL_STRONG)


def test_user_confirmation_is_the_only_path_to_one(pg_session, user_id):
    resolve_or_create(
        user_id, pg_session, kind=EntityKind.PERSON, name="王五", seen_at=NOW, evidence_event_id=1
    )
    assert confirm_alias(user_id, pg_session, alias="王五", alias_type=AliasType.NAME) is True
    row = alias_row(pg_session, user_id, "王五", AliasType.NAME)
    assert float(row.confidence) == pytest.approx(CONFIRMED)


def test_confirming_unknown_alias_reports_miss(pg_session, user_id):
    # 返回 False 而不是抛:App 上点确认时那条别名可能刚被别的流程改掉
    assert confirm_alias(user_id, pg_session, alias="没这个人", alias_type=AliasType.NAME) is False


def test_search_matches_canonical_name_and_alias(pg_session, user_id):
    created = resolve_or_create(
        user_id,
        pg_session,
        kind=EntityKind.PERSON,
        name="张三",
        seen_at=NOW,
        identifier="zhang@qq.com",
        identifier_type=AliasType.EMAIL,
        evidence_event_id=1,
    ).entity
    link_alias(
        user_id,
        pg_session,
        entity_id=created.id,
        alias="老张",
        alias_type=AliasType.NAME,
        evidence_event_id=1,
    )

    assert [e.id for e in search_entities(user_id, pg_session, name="老张")] == [created.id]
    assert [e.id for e in search_entities(user_id, pg_session, name="张")] == [created.id]
    assert search_entities(user_id, pg_session, name="查无此人") == []


def test_entities_are_isolated_per_user(pg_session, user_id):
    """铁律 1 的实测:别的用户的实体一条都看不见。"""
    import uuid

    other = str(uuid.uuid4())
    pg_session.execute(
        text("INSERT INTO users (id, display_name, wecom_userid) VALUES (:id, :n, :w)"),
        {"id": other, "n": "另一个人", "w": f"other-{other[:8]}"},
    )
    mine = resolve_or_create(
        user_id, pg_session, kind=EntityKind.PERSON, name="张三", seen_at=NOW, evidence_event_id=1
    ).entity

    assert find_by_alias(other, pg_session, alias="张三", alias_type=AliasType.NAME) is None
    assert get_entity(other, pg_session, entity_id=mine.id) is None
    assert search_entities(other, pg_session, name="张三") == []


def test_blank_name_is_rejected(pg_session, user_id):
    with pytest.raises(AliasError):
        resolve_or_create(user_id, pg_session, kind=EntityKind.PERSON, name="   ", seen_at=NOW)
