"""`transactions` 仓储的集成测试(06 §2.5 / §2.6)。需要真实 PostgreSQL。

**这一组几乎全在测去重**,因为那是这张表上唯一会静默出错的地方:

- 多记一笔:月底数字偏大,你会觉得"怎么花了这么多",但查得出来
- **少记一笔:你只会觉得这个月花得少** —— 没有任何迹象指向 bug

所以跨渠道合并必须是查询判定 + 可回溯记录,**不能是唯一约束**:
便利店连买两次同价的东西是真实存在的,约束会把第二笔悄悄吃掉。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from lifein.repos.transactions import (
    Direction,
    Stage,
    TransactionError,
    TxnKind,
    backfill,
    find_reconcilable,
    get_by_event,
    is_reconciled,
    list_between,
    record,
    spending_by_category,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def an_event(session, user_id, *, external_id: str) -> int:
    return session.execute(
        text(
            "INSERT INTO raw_events (user_id, source, external_id, occurred_at, trust, raw)"
            " VALUES (:u, 'notification', :e, :t, 'external', '{}'::jsonb) RETURNING id"
        ),
        {"u": user_id, "e": external_id, "t": NOW},
    ).scalar_one()


def a_txn(session, user_id, *, external_id="e1", **overrides):
    payload = {
        "occurred_at": NOW,
        "amount": Decimal("38.50"),
        "direction": Direction.DEBIT,
        "kind": TxnKind.EXPENSE,
        "channel": "alipay",
        "confidence": 0.9,
        "merchant_raw": "财付通",
        "account_hint": "1234",
    }
    payload.update(overrides)
    return record(
        user_id,
        session,
        source_event_id=an_event(session, user_id, external_id=external_id),
        **payload,
    )


class TestTheTwoKindsOfDeduplication:
    def test_the_same_notification_twice_records_once(self, pg_session, user_id):
        """采集器重试、网络重投 —— 靠 UNIQUE (user_id, source_event_id) 挡。"""
        event_id = an_event(pg_session, user_id, external_id="retry")
        first = record(
            user_id,
            pg_session,
            occurred_at=NOW,
            amount=Decimal("38.50"),
            direction=Direction.DEBIT,
            kind=TxnKind.EXPENSE,
            channel="alipay",
            source_event_id=event_id,
            confidence=0.9,
        )
        second = record(
            user_id,
            pg_session,
            occurred_at=NOW,
            amount=Decimal("38.50"),
            direction=Direction.DEBIT,
            kind=TxnKind.EXPENSE,
            channel="alipay",
            source_event_id=event_id,
            confidence=0.9,
        )

        assert first.created is True
        assert second.created is False and second.duplicate is True
        assert second.transaction.id == first.transaction.id

    def test_two_channels_one_purchase_get_merged(self, pg_session, user_id):
        """支付宝通知 + 银行短信 = 同一笔。**这是查询判定,不是约束。**"""
        alipay = a_txn(
            pg_session, user_id, external_id="ali", channel="alipay", account_hint=None
        )
        sms = a_txn(
            pg_session,
            user_id,
            external_id="sms",
            channel="bank_sms",
            occurred_at=NOW + timedelta(minutes=2),
            merchant_raw=None,
            account_hint="1234",
        )

        assert sms.created is False
        assert sms.merged_into == alipay.transaction.id
        # 合并要能回溯:这一笔是从哪几条通知拼出来的
        merged = get_by_event(
            user_id, pg_session, source_event_id=alipay.transaction.source_event_id
        )
        assert len(merged.merged_from_event_ids) == 1
        # 互相补齐:支付宝那条有商户没卡号,短信那条反过来
        assert merged.merchant_raw == "财付通"
        assert merged.account_hint == "1234"

    def test_two_real_purchases_at_the_same_price_are_not_merged(self, pg_session, user_id):
        """**便利店连买两次同价的东西。** 这正是不能用唯一约束的原因。"""
        first = a_txn(pg_session, user_id, external_id="buy1", channel="alipay")
        second = a_txn(
            pg_session,
            user_id,
            external_id="buy2",
            channel="alipay",  # 同一个渠道 —— 不是"两个渠道各报一次"
            occurred_at=NOW + timedelta(minutes=1),
        )

        assert first.created and second.created
        assert first.transaction.id != second.transaction.id

    def test_outside_the_window_is_a_separate_purchase(self, pg_session, user_id):
        a_txn(pg_session, user_id, external_id="a", channel="alipay")
        later = a_txn(
            pg_session,
            user_id,
            external_id="b",
            channel="bank_sms",
            occurred_at=NOW + timedelta(minutes=30),
        )
        assert later.created is True

    def test_a_different_card_is_a_different_purchase(self, pg_session, user_id):
        a_txn(pg_session, user_id, external_id="c1", channel="alipay", account_hint="1234")
        other = a_txn(
            pg_session,
            user_id,
            external_id="c2",
            channel="bank_sms",
            account_hint="9999",
            occurred_at=NOW + timedelta(minutes=1),
        )
        assert other.created is True


class TestWhatCountsAsSpending:
    def test_only_expense_is_counted(self, pg_session, user_id):
        """还款算进支出就是双重记账 —— 消费那一刻已经记过一次了。"""
        a_txn(pg_session, user_id, external_id="s1", amount=Decimal("100"), category="吃饭")
        a_txn(
            pg_session,
            user_id,
            external_id="s2",
            amount=Decimal("5000"),
            kind=TxnKind.REPAYMENT,
            channel="bank_app",
            occurred_at=NOW + timedelta(hours=1),
        )
        a_txn(
            pg_session,
            user_id,
            external_id="s3",
            amount=Decimal("200"),
            kind=TxnKind.REFUND,
            direction=Direction.CREDIT,
            channel="bank_app",
            occurred_at=NOW + timedelta(hours=2),
        )

        totals = spending_by_category(
            user_id, pg_session, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1)
        )
        assert totals == [("吃饭", Decimal("100.00"), 1)]

    def test_kind_knows_what_counts(self):
        assert TxnKind.EXPENSE.counts_as_spending
        for kind in (TxnKind.INCOME, TxnKind.TRANSFER, TxnKind.REFUND, TxnKind.REPAYMENT):
            assert not kind.counts_as_spending


class TestRefusedBeforeItHitsTheDatabase:
    def test_negative_amount_is_refused(self, pg_session, user_id):
        """方向由 direction 表达。混着来的话退款和支出在求和时会互相抵消。"""
        with pytest.raises(TransactionError):
            a_txn(pg_session, user_id, external_id="neg", amount=Decimal("-10"))

    def test_naive_timestamp_is_refused(self, pg_session, user_id):
        with pytest.raises(TransactionError):
            a_txn(pg_session, user_id, external_id="naive", occurred_at=datetime(2026, 9, 8, 12))

    def test_confidence_out_of_range_is_refused(self, pg_session, user_id):
        with pytest.raises(TransactionError):
            a_txn(pg_session, user_id, external_id="conf", confidence=1.5)


class TestReading:
    def test_list_between_is_newest_first(self, pg_session, user_id):
        a_txn(pg_session, user_id, external_id="r1", amount=Decimal("10"))
        a_txn(
            pg_session,
            user_id,
            external_id="r2",
            amount=Decimal("20"),
            occurred_at=NOW + timedelta(hours=3),
        )

        rows = list_between(
            user_id, pg_session, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1)
        )
        assert [r.amount for r in rows] == [Decimal("20.00"), Decimal("10.00")]

    def test_realtime_is_the_default_stage(self, pg_session, user_id):
        """实时通道落下的那一刻,商户名多半只是代收机构 —— 要等月度账单回填。"""
        result = a_txn(pg_session, user_id, external_id="stage")
        assert result.transaction.stage is Stage.REALTIME


class TestReconciliation:
    """对账回填(06 §2.6 第三层,ADR-012 的两阶段入账)。

    **这一组盯的是幂等。** ADR-012 写着"重复入账比漏记更糟"——
    漏记你会发现,重复不会。而同一封对账单被重新解一遍是常态:
    补跑、手动重导都会走到这里。
    """

    def a_statement_line(self, session, user_id, *, external_id="stmt-1") -> int:
        return an_event(session, user_id, external_id=external_id)

    def test_a_statement_line_finds_the_realtime_row(self, pg_session, user_id):
        a_txn(pg_session, user_id, external_id="rt-1")

        found = find_reconcilable(
            user_id, pg_session, amount=Decimal("38.50"),
            occurred_at=NOW + timedelta(days=1), account_hint="1234",
        )
        assert found is not None and found.stage is Stage.REALTIME

    def test_the_window_is_days_not_minutes(self, pg_session, user_id):
        """**对账单上记的往往是入账日而不是消费日**,周末和节假日能差几天。"""
        a_txn(pg_session, user_id, external_id="rt-1")

        assert find_reconcilable(
            user_id, pg_session, amount=Decimal("38.50"),
            occurred_at=NOW + timedelta(days=2), account_hint="1234",
        ) is not None
        assert find_reconcilable(
            user_id, pg_session, amount=Decimal("38.50"),
            occurred_at=NOW + timedelta(days=9), account_hint="1234",
        ) is None

    def test_a_different_card_is_not_the_same_transaction(self, pg_session, user_id):
        """窗口开大之后,金额和卡号是仅剩的两道判据 —— 卡号对不上就不是它。"""
        a_txn(pg_session, user_id, external_id="rt-1")

        assert find_reconcilable(
            user_id, pg_session, amount=Decimal("38.50"),
            occurred_at=NOW, account_hint="9999",
        ) is None

    def test_backfill_fills_in_the_real_merchant(self, pg_session, user_id):
        """实时那条的商户名是"财付通",归类价值为零 —— 回填的就是这个。"""
        created = a_txn(pg_session, user_id, external_id="rt-1")
        line = self.a_statement_line(pg_session, user_id)

        after = backfill(
            user_id, pg_session, txn_id=created.transaction.id, statement_event_id=line,
            merchant_raw="星巴克", order_no="2026090812345", category="餐饮",
        )

        assert after.merchant_raw == "星巴克"
        assert after.order_no == "2026090812345"
        assert after.category == "餐饮"
        assert after.stage is Stage.RECONCILED
        assert after.matched_statement_event_id == line

    def test_backfill_never_touches_the_money(self, pg_session, user_id):
        """**金额、时间、方向一律不改。** 对账单上的时间往往是入账日,
        拿它覆盖消费日会让一笔周末的消费跑到周一去,而月度报表按天切。"""
        created = a_txn(pg_session, user_id, external_id="rt-1")
        line = self.a_statement_line(pg_session, user_id)

        after = backfill(
            user_id, pg_session, txn_id=created.transaction.id,
            statement_event_id=line, merchant_raw="星巴克",
        )

        before = created.transaction
        assert (after.amount, after.occurred_at, after.direction, after.kind) == (
            before.amount, before.occurred_at, before.direction, before.kind,
        )

    def test_a_row_can_only_be_backfilled_once(self, pg_session, user_id):
        """幂等的第二道。并发跑两遍时只有一遍能改到行,另一遍拿到 None。"""
        created = a_txn(pg_session, user_id, external_id="rt-1")
        first_line = self.a_statement_line(pg_session, user_id, external_id="stmt-1")
        second_line = self.a_statement_line(pg_session, user_id, external_id="stmt-2")

        assert backfill(
            user_id, pg_session, txn_id=created.transaction.id,
            statement_event_id=first_line, merchant_raw="星巴克",
        ) is not None
        assert backfill(
            user_id, pg_session, txn_id=created.transaction.id,
            statement_event_id=second_line, merchant_raw="麦当劳",
        ) is None

    def test_a_reconciled_row_is_not_offered_again(self, pg_session, user_id):
        """对过的不再参与匹配 —— 否则重跑只会把一笔的商户名改成另一笔的,
        而那种错误在报表上看不出来。"""
        created = a_txn(pg_session, user_id, external_id="rt-1")
        line = self.a_statement_line(pg_session, user_id)
        backfill(
            user_id, pg_session, txn_id=created.transaction.id,
            statement_event_id=line, merchant_raw="星巴克",
        )

        assert find_reconcilable(
            user_id, pg_session, amount=Decimal("38.50"),
            occurred_at=NOW, account_hint="1234",
        ) is None

    def test_is_reconciled_is_the_first_gate(self, pg_session, user_id):
        """幂等的第一道:同一行对账单再解一遍时,连匹配都不用做。"""
        created = a_txn(pg_session, user_id, external_id="rt-1")
        line = self.a_statement_line(pg_session, user_id)

        assert is_reconciled(user_id, pg_session, statement_event_id=line) is False
        backfill(
            user_id, pg_session, txn_id=created.transaction.id,
            statement_event_id=line, merchant_raw="星巴克",
        )
        assert is_reconciled(user_id, pg_session, statement_event_id=line) is True

    def test_reconciliation_does_not_reach_across_users(self, pg_session, user_id):
        """铁律 1。对账单里带着一个人一整月去过哪些店。"""
        a_txn(pg_session, user_id, external_id="rt-1")
        other = "99999999-9999-9999-9999-999999999999"

        assert find_reconcilable(
            other, pg_session, amount=Decimal("38.50"),
            occurred_at=NOW, account_hint="1234",
        ) is None

    def test_a_line_that_matches_nothing_is_recorded_as_new(self, pg_session, user_id):
        """ADR-012:匹配不上的补为新记录。**它意味着实时那一路漏了一笔**,
        而漏记是这条链路唯一不会自己暴露的错误。"""
        line = self.a_statement_line(pg_session, user_id)

        result = record(
            user_id, pg_session, occurred_at=NOW, amount=Decimal("128.00"),
            direction=Direction.DEBIT, kind=TxnKind.EXPENSE, channel="statement",
            source_event_id=line, confidence=1.0, merchant_raw="全家便利店",
            order_no="ORD-9", stage=Stage.RECONCILED,
        )

        assert result.created is True
        assert result.transaction.stage is Stage.RECONCILED
        assert result.transaction.order_no == "ORD-9"

    def test_a_reconciled_record_never_joins_the_five_minute_merge(self, pg_session, user_id):
        """**对账补录不走跨渠道合并。**

        跨渠道合并是给实时通知用的:同一笔被支付宝和银行各推一条。而一条
        对账单行走到补录这一步,说明对账那一遍(3 天窗口、只认没对过的实时
        记录)已经判过"它不是已有的任何一笔"—— 再用一个更弱的规则去推翻
        那个判断,结果是把便利店连买两次同价商品里的第二笔悄悄吃掉。
        """
        a_txn(pg_session, user_id, external_id="rt-1")
        line = an_event(pg_session, user_id, external_id="stmt-1")

        result = record(
            user_id, pg_session, occurred_at=NOW, amount=Decimal("38.50"),
            direction=Direction.DEBIT, kind=TxnKind.EXPENSE, channel="statement",
            source_event_id=line, confidence=1.0, account_hint="1234",
            stage=Stage.RECONCILED,
        )

        assert result.created is True
        assert result.merged_into is None
