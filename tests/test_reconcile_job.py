"""对账 job(P2 第 6 片)。需要真实 PostgreSQL。

这一组盯三件事,按糟糕程度排:

1. **重复入账**。ADR-012 写着"重复入账比漏记更糟 —— 漏记你会发现,
   重复不会"。同一封对账单被重新解一遍是常态(补跑、手动重导),
   所以幂等要在重跑、并发、同一封里出现两行一样的三种情况下都成立。
2. **回填改了不该改的**。金额、时间、方向一个都不能动:对账单上的时间是
   入账日,拿它覆盖消费日会让一笔周末的消费跑到周一,而月度报表按天切。
3. **匹配认错人**。窗口开到 3 天之后,金额和卡号是仅剩的判据。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from lifein.jobs import reconcile
from lifein.jobs.reconcile import ReconcileDeps
from lifein.repos import merchant_rules, transactions
from lifein.repos.transactions import Direction, Stage, TxnKind
from lifein.sources import statement

pytestmark = pytest.mark.integration

BOUGHT_AT = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class RecordingAlerter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def alert(self, title: str, body: str) -> None:
        self.sent.append((title, body))


@pytest.fixture(autouse=True)
def tools_registered():
    from lifein.bootstrap import register_tools

    register_tools()


def a_realtime_txn(
    session,
    user_id,
    *,
    external_id: str = "rt-1",
    amount: str = "38.50",
    account_hint: str | None = "1234",
    occurred_at: datetime = BOUGHT_AT,
    merchant: str = "财付通",
    category: str | None = "其他",
):
    """实时通道落下的那一笔。**商户名是代收机构** —— 那正是要回填的理由。"""
    event_id = session.execute(
        text(
            "INSERT INTO raw_events (user_id, source, external_id, occurred_at, trust, raw)"
            " VALUES (:u, 'notification', :e, :t, 'external', '{}'::jsonb) RETURNING id"
        ),
        {"u": user_id, "e": external_id, "t": occurred_at},
    ).scalar_one()
    return transactions.record(
        user_id,
        session,
        occurred_at=occurred_at,
        amount=Decimal(amount),
        direction=Direction.DEBIT,
        kind=TxnKind.EXPENSE,
        channel="bank_sms",
        source_event_id=event_id,
        confidence=0.95,
        merchant_raw=merchant,
        category=category,
        account_hint=account_hint,
    ).transaction


def a_statement_line(
    session,
    user_id,
    *,
    external_id: str = "stmt-1",
    amount: str = "38.50",
    account_hint: str | None = "1234",
    merchant: str = "星巴克",
    order_no: str | None = "ORD-1",
    kind: str | None = "expense",
    occurred_at: datetime = BOUGHT_AT,
    channel: str = statement.CHANNEL,
) -> int:
    normalized = {
        "kind": "transaction",
        "title": merchant,
        "occurred_at": occurred_at.isoformat(),
        "external_ref": {"source": "email", "external_id": external_id},
        "trust": "external",
        "confidence": 1.0,
        "amount": {"value": amount, "currency": "CNY", "direction": "debit"},
    }
    raw = {
        "channel": channel,
        "statement": {"issuer": "cmb", "period": "2026-08"},
        "parsed": {
            "amount": amount,
            "account_hint": account_hint,
            "merchant_raw": merchant,
            "order_no": order_no,
            "kind": kind,
            "text_redacted": True,
        },
    }
    return session.execute(
        text(
            "INSERT INTO raw_events (user_id, source, external_id, occurred_at, trust,"
            " raw, normalized) VALUES (:u, 'email', :e, :t, 'external',"
            " CAST(:r AS JSONB), CAST(:n AS JSONB)) RETURNING id"
        ),
        {
            "u": user_id,
            "e": external_id,
            "t": occurred_at,
            "r": json.dumps(raw, ensure_ascii=False),
            "n": json.dumps(normalized, ensure_ascii=False),
        },
    ).scalar_one()


def run(session, user_id, *, alerter=None):
    return reconcile.run_once(
        user_id, session, deps=ReconcileDeps(alerter=alerter or RecordingAlerter())
    )


class TestBackfilling:
    def test_the_real_merchant_replaces_the_intermediary(self, pg_session, user_id):
        """**这个 job 存在的理由。** "财付通"归不出类,"星巴克"能。"""
        txn = a_realtime_txn(pg_session, user_id)
        a_statement_line(pg_session, user_id)

        result = run(pg_session, user_id)

        assert result.backfilled == 1
        after = transactions.get_by_event(
            user_id, pg_session, source_event_id=txn.source_event_id
        )
        assert after.merchant_raw == "星巴克"
        assert after.order_no == "ORD-1"
        assert after.stage is Stage.RECONCILED

    def test_the_money_is_never_touched(self, pg_session, user_id):
        """对账单上的时间是入账日。拿它覆盖消费日会让周末的消费跑到周一。"""
        txn = a_realtime_txn(pg_session, user_id)
        a_statement_line(pg_session, user_id, occurred_at=BOUGHT_AT + timedelta(days=2))

        run(pg_session, user_id)

        after = transactions.get_by_event(
            user_id, pg_session, source_event_id=txn.source_event_id
        )
        assert (after.amount, after.occurred_at, after.direction, after.kind) == (
            txn.amount, txn.occurred_at, txn.direction, txn.kind,
        )

    def test_the_rules_table_learns_from_the_real_name(self, pg_session, user_id):
        """**规则表真正的沉淀从这里开始**(ADR-008)。"""
        a_realtime_txn(pg_session, user_id, category="餐饮")
        a_statement_line(pg_session, user_id)

        result = run(pg_session, user_id)

        assert result.rules_learned == 1
        (rule,) = merchant_rules.list_rules(user_id, pg_session)
        assert (rule.pattern, rule.category) == ("星巴克", "餐饮")

    def test_an_existing_rule_recategorizes(self, pg_session, user_id):
        """归类跑两次,第二次才是有价值的那次:实时那遍归的是代收机构。"""
        merchant_rules.remember(user_id, pg_session, merchant="星巴克", category="餐饮")
        txn = a_realtime_txn(pg_session, user_id, category="其他")
        a_statement_line(pg_session, user_id)

        result = run(pg_session, user_id)

        after = transactions.get_by_event(
            user_id, pg_session, source_event_id=txn.source_event_id
        )
        assert after.category == "餐饮"
        assert result.recategorized == 1

    def test_no_rule_keeps_the_temporary_category(self, pg_session, user_id):
        """规则没命中就留着实时那遍的临时分类。**空着会让这笔从报表里掉出去。**"""
        txn = a_realtime_txn(pg_session, user_id, category="购物")
        a_statement_line(pg_session, user_id, merchant="没见过的店")

        run(pg_session, user_id)

        after = transactions.get_by_event(
            user_id, pg_session, source_event_id=txn.source_event_id
        )
        assert after.category == "购物"


class TestIdempotence:
    """**重复入账比漏记更糟** —— 漏记你会发现,重复不会。"""

    def test_running_twice_changes_nothing(self, pg_session, user_id):
        a_realtime_txn(pg_session, user_id)
        a_statement_line(pg_session, user_id)

        first = run(pg_session, user_id)
        second = run(pg_session, user_id)

        assert (first.backfilled, second.backfilled) == (1, 0)
        assert second.lines_seen == 0  # 取的时候就被滤掉了
        assert _ledger_size(pg_session, user_id) == 1

    def test_a_line_that_matched_nothing_is_not_recorded_twice(self, pg_session, user_id):
        a_statement_line(pg_session, user_id)

        first = run(pg_session, user_id)
        second = run(pg_session, user_id)

        assert (first.recorded_new, second.recorded_new) == (1, 0)
        assert _ledger_size(pg_session, user_id) == 1

    def test_two_identical_lines_in_one_statement(self, pg_session, user_id):
        """便利店连买两次同价的东西是真实存在的 —— 对账单上就是两行。
        **第一行对掉实时那笔,第二行补成新的**,总数还是两笔。"""
        a_realtime_txn(pg_session, user_id)
        a_statement_line(pg_session, user_id, external_id="stmt-1")
        a_statement_line(pg_session, user_id, external_id="stmt-2", order_no="ORD-2")

        result = run(pg_session, user_id)

        assert (result.backfilled, result.recorded_new) == (1, 1)
        assert _ledger_size(pg_session, user_id) == 2

    def test_a_realtime_row_is_only_backfilled_once(self, pg_session, user_id):
        """对过的不再参与匹配,否则重跑只会把商户名改成另一行的。"""
        txn = a_realtime_txn(pg_session, user_id)
        a_statement_line(pg_session, user_id, external_id="stmt-1", merchant="星巴克")
        run(pg_session, user_id)

        a_statement_line(pg_session, user_id, external_id="stmt-2", merchant="麦当劳")
        run(pg_session, user_id)

        after = transactions.get_by_event(
            user_id, pg_session, source_event_id=txn.source_event_id
        )
        assert after.merchant_raw == "星巴克"


class TestMatching:
    def test_a_few_days_apart_still_matches(self, pg_session, user_id):
        """**对账单上记的往往是入账日**,周末和节假日能差几天。"""
        a_realtime_txn(pg_session, user_id)
        a_statement_line(pg_session, user_id, occurred_at=BOUGHT_AT + timedelta(days=2))

        assert run(pg_session, user_id).backfilled == 1

    def test_a_different_amount_is_a_different_transaction(self, pg_session, user_id):
        a_realtime_txn(pg_session, user_id, amount="38.50")
        a_statement_line(pg_session, user_id, amount="39.50")

        result = run(pg_session, user_id)
        assert (result.backfilled, result.recorded_new) == (0, 1)

    def test_a_different_card_is_a_different_transaction(self, pg_session, user_id):
        a_realtime_txn(pg_session, user_id, account_hint="1234")
        a_statement_line(pg_session, user_id, account_hint="9999")

        assert run(pg_session, user_id).backfilled == 0


class TestLinesThatDoNotBecomeTransactions:
    def test_a_line_without_a_kind_is_not_recorded(self, pg_session, user_id):
        """**debit 既可能是消费也可能是还款。** 按方向兜底会把还款记成消费,
        而那正是"误记率 = 0"点名要挡的。宁可这一笔不入账。"""
        a_statement_line(pg_session, user_id, kind=None)

        result = run(pg_session, user_id)

        assert result.recorded_new == 0
        assert _ledger_size(pg_session, user_id) == 0
        assert result.warnings

    def test_a_kind_outside_the_enum_is_treated_as_missing(self, pg_session, user_id):
        """不猜一个最接近的 —— 和记账 agent 第 4 层同一条规矩。"""
        a_statement_line(pg_session, user_id, kind="刷卡消费")

        assert run(pg_session, user_id).recorded_new == 0

    def test_a_malformed_line_is_counted_and_alerted(self, pg_session, user_id):
        """**不是脏数据,是解析器和 statement.py 对不上**,要修。"""
        event_id = a_statement_line(pg_session, user_id)
        pg_session.execute(
            text("UPDATE raw_events SET raw = raw - 'parsed' WHERE id = :i"), {"i": event_id}
        )
        alerter = RecordingAlerter()

        result = run(pg_session, user_id, alerter=alerter)

        assert result.unparsable == 1
        assert any("解析" in title for title, _ in alerter.sent)

    def test_realtime_notifications_are_left_alone(self, pg_session, user_id):
        """只处理 channel=statement 的。实时那条链路一个字节都不该被碰。"""
        a_statement_line(pg_session, user_id, channel="bank_sms")

        result = run(pg_session, user_id)
        assert result.lines_seen == 0


class TestCoverage:
    def test_coverage_is_the_share_that_realtime_already_had(self, pg_session, user_id):
        """03 的指标:对账单里有多少笔曾在实时通道出现过。"""
        a_realtime_txn(pg_session, user_id, external_id="rt-1", amount="38.50")
        a_statement_line(pg_session, user_id, external_id="s-1", amount="38.50")
        a_statement_line(
            pg_session, user_id, external_id="s-2", amount="128.00", order_no="ORD-2"
        )

        result = run(pg_session, user_id)

        assert (result.backfilled, result.recorded_new) == (1, 1)
        assert result.coverage == 0.5

    def test_nothing_to_reconcile_is_unknown_not_perfect(self, pg_session, user_id):
        """**"全覆盖"和"这个月还没对过账"不该长成同一个数字。**"""
        assert run(pg_session, user_id).coverage is None


def test_reconciliation_does_not_reach_across_users(pg_session, user_id):
    """铁律 1。对账单里带着一个人一整月去过哪些店。"""
    a_realtime_txn(pg_session, user_id)
    a_statement_line(pg_session, user_id)
    other = "99999999-9999-9999-9999-999999999999"

    result = run(pg_session, other)

    assert result.lines_seen == 0
    after = transactions.list_between(
        user_id, pg_session, start=BOUGHT_AT - timedelta(days=1),
        end=BOUGHT_AT + timedelta(days=1),
    )
    assert after[0].stage is Stage.REALTIME


def _ledger_size(session, user_id) -> int:
    return session.execute(
        text("SELECT count(*) FROM transactions WHERE user_id = :u"), {"u": user_id}
    ).scalar_one()
