"""别名归一化与置信度规则 —— 纯函数,不需要数据库。

这组测试盯的是记忆层最容易悄悄出错的地方:**同一个人被算成两个**。
它不报错、不抛异常,只是从某天起问"上次和张三聊的是什么"少了一半结果。
"""

from __future__ import annotations

import pytest

from lifein.models.normalized import IdentifierType
from lifein.repos.entities import (
    INITIAL_NAME,
    INITIAL_STRONG,
    MAX_INFERRED,
    AliasType,
    alias_type_for_identifier,
    initial_confidence,
    normalize_alias,
    promote_confidence,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Zhang@QQ.com", "zhang@qq.com"),
        ("  zhang@qq.com  ", "zhang@qq.com"),
        ("zhang @qq.com", "zhang@qq.com"),
    ],
)
def test_email_folds_to_one_key(raw: str, expected: str):
    assert normalize_alias(raw, AliasType.EMAIL) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+86 138-0000-0000", "13800000000"),
        ("138 0000 0000", "13800000000"),
        ("(138)00000000", "13800000000"),
    ],
)
def test_phone_folds_to_one_key(raw: str, expected: str):
    assert normalize_alias(raw, AliasType.PHONE) == expected


def test_name_keeps_case_but_collapses_whitespace():
    # 英文名压小写就没有原样可查了,而中文名根本没有大小写
    assert normalize_alias("  Li  Ming ", AliasType.NAME) == "Li Ming"
    assert normalize_alias("张 三", AliasType.NAME) == "张 三"


def test_blank_alias_yields_empty_key():
    # 空键不许落库。返回空串而不是抛,是因为判断"这条别名能不能用"的地方
    # 有好几处,让它们各自决定是拒绝还是跳过
    assert normalize_alias("   ", AliasType.NAME) == ""


def test_card_last4_is_not_an_alias():
    """卡号后四位标识的是一张卡,不是一个人。

    映射过去会把两个碰巧同尾号的账户归并成同一个实体 —— 那是记忆污染,
    而且是那种查不出来源的污染。
    """
    assert alias_type_for_identifier(IdentifierType.CARD_LAST4) is None
    assert alias_type_for_identifier(IdentifierType.EMAIL) is AliasType.EMAIL
    assert alias_type_for_identifier(IdentifierType.WECOM_USERID) is AliasType.WECOM_USERID


def test_strong_identifier_starts_high_and_does_not_climb():
    assert initial_confidence(AliasType.EMAIL) == INITIAL_STRONG
    # 再来一百封邮件,邮箱也不会比第一封时更"精确"
    assert promote_confidence(AliasType.EMAIL, 100) == INITIAL_STRONG


def test_name_climbs_with_evidence_and_stops_below_confirmed():
    assert promote_confidence(AliasType.NAME, 1) == pytest.approx(INITIAL_NAME)
    assert promote_confidence(AliasType.NAME, 3) == pytest.approx(0.7)
    # 封顶 0.9:1.0 那一档留给用户确认,不留就分不出"它猜的"和"你点过的"
    assert promote_confidence(AliasType.NAME, 50) == pytest.approx(MAX_INFERRED)


def test_no_evidence_does_not_go_below_initial():
    # 抽取时拿不到 raw_events.id 是允许的(比如从用户输入直接建),
    # 那时置信度就该是初始值,不能被算成负数或 0
    assert promote_confidence(AliasType.NAME, 0) == pytest.approx(INITIAL_NAME)
