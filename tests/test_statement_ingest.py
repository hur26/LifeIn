"""对账单 → `raw_events`(P2 第 6/7/8 片之间那一段)。

**这一组几乎全在盯 `external_id`。** 它是 `UNIQUE (user_id, source, external_id)`
唯一的输入,而那个唯一键是挡住"重导一次变成两份账"的全部依靠。
ADR-012 写着重复入账比漏记更糟 —— 漏记你会发现,重复不会。

但它也不能反过来太狠:**同一份账单里出现两行完全一样的是真实存在的**
(便利店连买两次同价商品),把第二笔当成重复吞掉,账本会少一笔,
而少一笔在报表上看不出来。这两件事就是这一组的全部内容。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

from lifein.models.normalized import EventKind, PartyRole, Trust
from lifein.repos.transactions import Direction, TxnKind
from lifein.sources import statement_ingest
from lifein.sources.statement import StatementRow

SHANGHAI = timezone(timedelta(hours=8))


def a_row(**overrides) -> StatementRow:
    payload = {
        "occurred_on": date(2026, 8, 20),
        "amount": Decimal("38.50"),
        "direction": Direction.DEBIT,
        "merchant_raw": "星巴克",
        "account_hint": "1234",
        "order_no": None,
        "kind": TxnKind.EXPENSE,
    }
    payload.update(overrides)
    return StatementRow(**payload)


def events(rows, *, issuer: str = "cmb", period: str | None = "2026-08"):
    return statement_ingest.to_events(rows, issuer=issuer, tz=SHANGHAI, period=period)


class TestOneEventPerLine:
    def test_two_hundred_lines_become_two_hundred_events(self):
        """**06 §2.6 第三层写死了这一条。** 共用一个 source_event_id
        只能入账一笔,一封带 200 行的对账单就只剩一笔。"""
        rows = [a_row(order_no=f"ORD-{i}") for i in range(200)]
        assert len(events(rows)) == 200

    def test_the_shape_matches_what_the_reconcile_job_reads(self):
        """产出的形状要能被 `statement.from_event()` 解回去 ——
        两边对不上的话对账 job 会把整封账单算成"解不出形状"。"""
        from lifein.repos.raw_events import StoredEvent
        from lifein.sources.statement import from_event

        (event,) = events([a_row(order_no="ORD-1")])
        line = from_event(StoredEvent(event_id=1, event=event.normalized, raw=event.raw))

        assert line is not None
        assert line.amount == Decimal("38.50")
        assert line.merchant_raw == "星巴克"
        assert line.account_hint == "1234"
        assert line.order_no == "ORD-1"
        assert line.kind is TxnKind.EXPENSE
        assert (line.issuer, line.period) == ("cmb", "2026-08")

    def test_it_is_a_transaction_event_with_the_merchant_as_a_party(self):
        (event,) = events([a_row()])

        assert event.normalized.kind is EventKind.TRANSACTION
        assert event.trust is Trust.EXTERNAL
        assert event.normalized.confidence == 1.0  # 对账单是金额权威源
        (party,) = event.normalized.parties
        assert (party.role, party.display_name) == (PartyRole.MERCHANT, "星巴克")

    def test_a_line_without_a_merchant_still_has_a_title(self):
        """`title` 不能为空(归一化骨架的硬要求),没商户就用发卡行。"""
        (event,) = events([a_row(merchant_raw=None)])
        assert event.normalized.title == "cmb"
        assert event.normalized.parties == []


class TestWhenItHappened:
    def test_the_date_lands_at_noon_not_midnight(self):
        """**落午夜会跨日界。** 一笔 8 月 20 日的消费在东八区是 20 日 00:00,
        换成 UTC 是 19 日 16:00 —— 而对账的 3 天窗口和月度报表都按天切。"""
        (event,) = events([a_row()])

        assert event.occurred_at == datetime(2026, 8, 20, 12, 0, tzinfo=SHANGHAI)
        # 换算到 UTC 还在同一天
        assert event.occurred_at.astimezone(UTC).date() == date(2026, 8, 20)

    def test_the_timezone_comes_from_the_caller(self):
        (event,) = statement_ingest.to_events(
            [a_row()], issuer="cmb", tz=UTC, period=None
        )
        assert event.occurred_at.tzinfo is UTC


class TestTheIdentityOfALine:
    """**重导一次不能变成两份账。**"""

    def test_the_same_line_twice_gets_the_same_id(self):
        first = events([a_row()])[0].external_id
        second = events([a_row()])[0].external_id
        assert first == second

    def test_an_order_number_is_the_identity_when_there_is_one(self):
        """支付平台给的,天然唯一且稳定 —— **换个解析路径也不会变。**"""
        (event,) = events([a_row(order_no="2026082012345")])
        assert event.external_id == "stmt:cmb:2026082012345"

    def test_the_order_number_survives_a_changed_merchant(self):
        """同一笔交易两次解出来的商户名可能不一样(截断、空格)。
        有订单号时那些都不该影响它的身份。"""
        with_name = events([a_row(order_no="ORD-1", merchant_raw="星巴克(国贸店)")])
        short = events([a_row(order_no="ORD-1", merchant_raw="星巴克")])
        assert with_name[0].external_id == short[0].external_id

    def test_without_an_order_number_the_content_is_the_identity(self):
        (event,) = events([a_row()])
        assert event.external_id.startswith("stmt:cmb:")

    def test_a_different_amount_is_a_different_line(self):
        one = events([a_row(amount=Decimal("38.50"))])[0].external_id
        other = events([a_row(amount=Decimal("39.50"))])[0].external_id
        assert one != other

    def test_a_different_issuer_is_a_different_line(self):
        """**同一笔从两个渠道来会是两条事件,这是对的。**
        对账那一步会把它们合起来,而在这里合会丢掉"两边各说了什么"。"""
        cmb = events([a_row(order_no=None)], issuer="cmb")[0].external_id
        alipay = events([a_row(order_no=None)], issuer="alipay")[0].external_id
        assert cmb != alipay

    def test_the_line_number_is_not_part_of_the_identity(self):
        """**不能用行号。** 同一份账单换个解析路径行号就变了,
        于是同一笔进两次。"""
        rows = [a_row(order_no="A"), a_row(order_no="B")]
        forward = [e.external_id for e in events(rows)]
        backward = [e.external_id for e in events(list(reversed(rows)))]
        assert set(forward) == set(backward)


class TestTwoIdenticalLinesInOneStatement:
    """**便利店连买两次同价商品是真实存在的。**

    唯一键挡重复导入,但它挡不住这个 —— 挡住了账本就少一笔,
    而少一笔在报表上看不出来。
    """

    def test_the_second_one_gets_its_own_id(self):
        first, second = events([a_row(), a_row()])
        assert first.external_id != second.external_id
        assert second.external_id.endswith("#2")

    def test_three_identical_lines_are_three_events(self):
        ids = {e.external_id for e in events([a_row(), a_row(), a_row()])}
        assert len(ids) == 3

    def test_re_importing_the_same_pair_is_still_the_same_pair(self):
        """**关键的一条。** 序号必须是确定的:同一份账单再导一次,
        两行还是那两个 id —— 靠随机数或时间戳的话重导会变成四笔。"""
        first = [e.external_id for e in events([a_row(), a_row()])]
        again = [e.external_id for e in events([a_row(), a_row()])]
        assert first == again


class TestWhatDoesNotGetStored:
    def test_the_original_cells_are_not_kept(self):
        """R10:交易类只留金额、时间、卡号后四位、商户。**对账单上还有余额、
        额度、积分**,记账一样都用不上,泄露出去的信息却比一笔消费多得多。"""
        row = a_row()
        row = StatementRow(
            occurred_on=row.occurred_on,
            amount=row.amount,
            direction=row.direction,
            merchant_raw=row.merchant_raw,
            account_hint=row.account_hint,
            kind=row.kind,
            raw_cells=("08-20", "星巴克", "38.50", "可用额度 49,000.00", "积分 385"),
        )
        (event,) = events([row])

        assert "49,000.00" not in str(event.raw)
        assert "积分" not in str(event.raw)
        assert event.raw["parsed"]["text_redacted"] is True


class TestReporting:
    def test_the_period_is_none_when_the_rows_span_months(self):
        """支付宝的导出可以自选区间。**硬给一个月份会让它变成假信息**,
        而那个字段是用来回答"八月的账单导过没有"的。"""
        rows = [a_row(occurred_on=date(2026, 8, 20)), a_row(occurred_on=date(2026, 9, 1))]
        assert statement_ingest.period_of(rows) is None
        assert statement_ingest.period_of([a_row()]) == "2026-08"

    def test_inbound_and_outbound_are_counted_apart(self):
        """一份账单里进账那几笔最容易出问题(退款和收入分不开),
        数字分开才看得出异常。"""
        rows = [a_row(order_no="A"), a_row(order_no="B", direction=Direction.CREDIT)]
        assert statement_ingest.summarize(events(rows)) == {
            "lines": 2,
            "outbound": 1,
            "inbound": 1,
        }

    def test_no_rows_is_all_zeros(self):
        assert statement_ingest.summarize([])["lines"] == 0
        assert statement_ingest.period_of([]) is None
