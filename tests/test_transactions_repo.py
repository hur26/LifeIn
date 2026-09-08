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
    get_by_event,
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
