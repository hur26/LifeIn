"""对账单行的形状(P2 第 6 片)。纯函数,不需要库。

**这个模块是给第 7、8 片的解析器用的契约**,所以这一组主要在钉两件事:

- 形状不对时**返回 None 而不是抛异常**(一行坏的不该让整封对账单停下),
  但也不静默 —— 调用方按计数报出来
- **`kind` 不按方向兜底**:debit 既可能是消费也可能是还款,
  而把还款记成消费就是双重记账
"""

from __future__ import annotations

from datetime import UTC, date, datetime
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
from lifein.repos.transactions import Direction as TxnDirection
from lifein.repos.transactions import TxnKind
from lifein.sources.statement import (
    card_tail,
    from_event,
    parse_amount,
    parse_date,
    parse_kind,
)

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


class TestSharedParsers:
    """第 7、8 片共用的那几个。**放在这里而不在某个解析器里**,
    是因为放在先写的那个里面,第二个就只能跟着长成那个样子。"""

    @pytest.mark.parametrize(
        ("text", "value", "direction"),
        [
            ("38.50", Decimal("38.50"), TxnDirection.DEBIT),
            ("1,234.56", Decimal("1234.56"), TxnDirection.DEBIT),
            ("¥38.50", Decimal("38.50"), TxnDirection.DEBIT),
            ("-38.50", Decimal("38.50"), TxnDirection.CREDIT),
        ],
    )
    def test_parse_amount(self, text, value, direction):
        """**负号只表示方向,不进金额。** 混着来的话"退款 -50"和"支出 50"
        在求和时会互相抵消,而它们是两件事。"""
        assert parse_amount(text) == (value, direction)

    @pytest.mark.parametrize("text", ["", "小计", "0.00", "-0.00", "余额12345"])
    def test_amounts_that_are_not_amounts(self, text):
        assert parse_amount(text) is None

    @pytest.mark.parametrize(
        ("fragments", "expected"),
        [
            (("消费",), TxnKind.EXPENSE),
            (("", "星巴克消费"), TxnKind.EXPENSE),
            (("退货",), TxnKind.REFUND),
            (("", "手机银行还款"), TxnKind.REPAYMENT),
            (("", "转账还款"), TxnKind.REPAYMENT),  # 还款排在转账前面
            # "退货款"里没有"还款"两个字,所以它就是退款 —— 这条钉住的是
            # 别把包含关系想当然:我第一版的注释写反了,测试当场就抓到了
            (("", "退货款"), TxnKind.REFUND),
            (("转入",), TxnKind.TRANSFER),
            (("", "工资收入"), TxnKind.INCOME),
        ],
    )
    def test_parse_kind_order_matters(self, fragments, expected):
        """**顺序不能按字典序排。** 真实的冲突是"转账还款":还款排在转账
        后面的话,一笔还款会被记成转账,而转账不进支出统计。"""
        assert parse_kind(*fragments) is expected

    @pytest.mark.parametrize("text", ["", "其他", "某某某", "利息"])
    def test_kinds_that_stay_unknown(self, text):
        """**"利息"故意认不出来。** 储蓄卡上是收入,信用卡上是费用,
        同两个字方向相反 —— 认错会让月度收入凭空多一笔。"""
        assert parse_kind(text) is None

    def test_parse_date_uses_the_given_year(self):
        """**不从今天推。** 一月收到的是去年十二月的账单,
        推出来的年份会让整份账单落到未来,而未来的交易不进任何月度报表。"""
        assert parse_date("12-31", year=2025) == date(2025, 12, 31)
        assert parse_date("2024-03-05", year=2026) == date(2024, 3, 5)

    @pytest.mark.parametrize("text", ["", "本期应还", "13-45"])
    def test_dates_that_do_not_parse(self, text):
        assert parse_date(text, year=2026) is None

    def test_card_tail(self):
        assert card_tail("招商银行储蓄卡(1234)") == "1234"
        assert card_tail("尾号 5678") == "5678"
        assert card_tail("没有数字") is None
