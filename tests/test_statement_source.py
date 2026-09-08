"""对账单行的形状(P2 第 6 片)。纯函数,不需要库。

**这个模块是给第 7、8 片的解析器用的契约**,所以这一组主要在钉两件事:

- 形状不对时**返回 None 而不是抛异常**(一行坏的不该让整封对账单停下),
  但也不静默 —— 调用方按计数报出来
- **`kind` 不按方向兜底**:debit 既可能是消费也可能是还款,
  而把还款记成消费就是双重记账
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lifein.models.normalized import (
    Amount,
    Direction,
    EventKind,
    ExternalRef,
    NormalizedEvent,
    Trust,
)
from lifein.repos.raw_events import StoredEvent
from lifein.repos.transactions import TxnKind
from lifein.sources.statement import from_event

BOUGHT_AT = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def a_line(*, raw: dict | None = None, amount: str = "38.50") -> StoredEvent:
    return StoredEvent(
        event_id=7,
        event=NormalizedEvent(
            kind=EventKind.TRANSACTION,
            title="星巴克",
            occurred_at=BOUGHT_AT,
            external_ref=ExternalRef(source="email", external_id="stmt-1"),
            trust=Trust.EXTERNAL,
            confidence=1.0,
            amount=Amount(value=Decimal(amount), currency="CNY", direction=Direction.DEBIT),
        ),
        raw=raw
        if raw is not None
        else {
            "channel": "statement",
            "statement": {"issuer": "cmb", "period": "2026-08"},
            "parsed": {
                "account_hint": "1234",
                "merchant_raw": "星巴克(国贸店)",
                "order_no": "ORD-1",
                "kind": "expense",
            },
        },
    )


def test_a_well_formed_line_parses():
    line = from_event(a_line())

    assert line.amount == Decimal("38.50")
    assert line.occurred_at == BOUGHT_AT
    assert (line.account_hint, line.order_no) == ("1234", "ORD-1")
    assert line.merchant_raw == "星巴克(国贸店)"
    assert line.kind is TxnKind.EXPENSE
    assert (line.issuer, line.period) == ("cmb", "2026-08")


def test_the_money_comes_from_the_normalized_event_not_parsed():
    """铁律 9,和实时那一路同一条规矩:金额、方向取归一化那份。"""
    raw = {"channel": "statement", "parsed": {"amount": "9999.00", "kind": "expense"}}
    assert from_event(a_line(raw=raw, amount="38.50")).amount == Decimal("38.50")


class TestKind:
    def test_a_missing_kind_stays_missing(self):
        """**不按方向兜底。** debit 既可能是消费也可能是还款。"""
        raw = {"channel": "statement", "parsed": {"account_hint": "1234"}}
        assert from_event(a_line(raw=raw)).kind is None

    def test_a_kind_outside_the_enum_is_dropped(self):
        """不猜一个最接近的 —— 和记账 agent 第 4 层同一条规矩。"""
        raw = {"channel": "statement", "parsed": {"kind": "刷卡消费"}}
        assert from_event(a_line(raw=raw)).kind is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("expense", TxnKind.EXPENSE), ("REPAYMENT", TxnKind.REPAYMENT),
         (" refund ", TxnKind.REFUND)],
    )
    def test_recognized_kinds(self, value, expected):
        raw = {"channel": "statement", "parsed": {"kind": value}}
        assert from_event(a_line(raw=raw)).kind is expected


class TestShapesThatDoNotParse:
    def test_no_parsed_block(self):
        assert from_event(a_line(raw={"channel": "statement"})) is None

    def test_no_raw_at_all(self):
        assert from_event(a_line(raw={})) is None

    def test_empty_strings_become_none(self):
        """空串和"没有"要长成同一个东西,否则规则表里会出现一条空 pattern。"""
        raw = {"channel": "statement", "parsed": {"merchant_raw": "  ", "order_no": ""}}
        line = from_event(a_line(raw=raw))
        assert (line.merchant_raw, line.order_no) == (None, None)
