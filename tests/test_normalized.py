"""归一化骨架的测试。

骨架是所有适配器的公共约束,它松一寸,七个数据源就各自跑偏一寸。
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from lifein.models.normalized import (
    TITLE_MAX,
    Amount,
    Direction,
    EventKind,
    ExternalRef,
    Flag,
    NormalizedEvent,
    Party,
    PartyRole,
    Trust,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
REF = ExternalRef(source="email", external_id="msg-1")


def event(**overrides) -> NormalizedEvent:
    base = dict(
        kind=EventKind.MESSAGE,
        title="季度报销单已通过",
        occurred_at=NOW,
        external_ref=REF,
        trust=Trust.EXTERNAL,
        confidence=0.9,
    )
    return NormalizedEvent(**{**base, **overrides})


def test_minimal_event():
    e = event()
    assert e.parties == [] and e.flags == []
    assert e.is_external is True


def test_naive_datetime_is_rejected():
    # 无时区的时间戳会让"昨天的邮件"这种窗口计算悄悄错一整天
    with pytest.raises(ValidationError):
        event(occurred_at=datetime(2026, 9, 7, 12, 0))


def test_long_title_is_truncated_and_flagged():
    e = event(title="标" * 300)
    assert len(e.title) == TITLE_MAX
    assert Flag.TRUNCATED in e.flags


def test_blank_title_is_rejected():
    with pytest.raises(ValidationError):
        event(title="   ")


def test_transaction_requires_amount():
    with pytest.raises(ValidationError):
        event(kind=EventKind.TRANSACTION)


def test_amount_only_on_transactions():
    amount = Amount(value=Decimal("12.30"), direction=Direction.DEBIT)
    with pytest.raises(ValidationError):
        event(kind=EventKind.MESSAGE, amount=amount)

    e = event(kind=EventKind.TRANSACTION, title="便利店", amount=amount)
    assert e.amount.currency == "CNY"


def test_identifier_without_type_is_rejected():
    # 有值没类型的标识符没法用来归并实体,等于白存
    with pytest.raises(ValidationError):
        Party(role=PartyRole.FROM, display_name="张三", identifier="a@b.com")


def test_unknown_field_is_rejected():
    # 落不上骨架的字段进 raw,不许往骨架上加
    with pytest.raises(ValidationError):
        event(merchant_code="9999")


def test_trust_has_exactly_two_values():
    # 一旦出现"半可信",调用方就会开始猜
    assert {t.value for t in Trust} == {"user_input", "external"}
