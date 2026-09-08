"""记账 job(P2 第 5 片的收尾)。需要真实 PostgreSQL。

这条链路第一次把前面几片接起来:事件 → 判定 → 复核 → 归类 → 入账/待确认。
用假仓储测没意义 —— 要验的正是**东西落到了哪张表里**。

一句话概括这一组盯什么:**账本上不该多出任何一笔**。所以用例大半在数
"没入账"的那些去向,以及三条路各自的计数对不对得上 ——
计数错了的表现很坑:日志说记了 5 笔,账本上只有 3 笔,而你无从判断
是丢了两笔还是数错了。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from lifein.jobs import bookkeeping
from lifein.jobs.bookkeeping import BookkeepingDeps
from lifein.repos import merchant_rules, pending, transactions

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 20, 15, tzinfo=UTC)
WINDOW_TIME = NOW - timedelta(hours=2)


class FakeLLM:
    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        return _Response(json.dumps({"items": self.items}, ensure_ascii=False))


class _Response:
    def __init__(self, content: str) -> None:
        self._content = content
        self.prompt_tokens = 100
        self.completion_tokens = 20

    def as_json(self):
        return json.loads(self._content)


class RecordingAlerter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def alert(self, title: str, body: str) -> None:
        self.sent.append((title, body))


@pytest.fixture(autouse=True)
def tools_registered():
    from lifein.bootstrap import register_tools

    register_tools()


def an_event(
    session,
    user_id,
    *,
    external_id: str = "t-1",
    amount: str = "38.50",
    merchant: str | None = "星巴克",
    channel: str = "bank_sms",
    account_hint: str | None = "1234",
    occurred_at: datetime = WINDOW_TIME,
) -> int:
    normalized = {
        "kind": "transaction",
        "title": "招商银行",
        "occurred_at": occurred_at.isoformat(),
        "external_ref": {"source": "notification", "external_id": external_id},
        "trust": "external",
        "confidence": 1.0,
        "amount": {"value": amount, "currency": "CNY", "direction": "debit"},
        "parties": (
            [{"role": "merchant", "display_name": merchant}] if merchant else []
        ),
    }
    raw = {
        "channel": channel,
        "parsed": {
            "amount": amount,
            "account_hint": account_hint,
            "merchant_raw": merchant,
            "text_redacted": True,
            "matched": f"消费人民币{amount}元",
        },
    }
    return session.execute(
        text(
            "INSERT INTO raw_events (user_id, source, external_id, occurred_at, trust,"
            " raw, normalized) VALUES (:u, 'notification', :x, :t, 'external',"
            " CAST(:r AS JSONB), CAST(:n AS JSONB)) RETURNING id"
        ),
        {
            "u": user_id,
            "x": external_id,
            "t": occurred_at,
            "r": json.dumps(raw, ensure_ascii=False),
            "n": json.dumps(normalized, ensure_ascii=False),
        },
    ).scalar_one()


def run(session, user_id, items, *, alerter=None):
    llm = FakeLLM(items)
    results = bookkeeping.run_once(
        user_id,
        session,
        deps=BookkeepingDeps(llm=llm, alerter=alerter or RecordingAlerter()),
        now=NOW,
    )
    return [r for r in results if not r.skipped], llm


def judged(ref: str = "pixel:t-1", **overrides) -> dict:
    return {
        "ref": ref,
        "judgment": "expense",
        "category": "餐饮",
        "confidence": 0.95,
        **overrides,
    }


class TestRecording:
    def test_a_confident_expense_lands_in_the_ledger(self, pg_session, user_id):
        event_id = an_event(pg_session, user_id)

        results, _ = run(pg_session, user_id, [judged("t-1")])

        assert sum(r.recorded for r in results) == 1
        txn = transactions.get_by_event(user_id, pg_session, source_event_id=event_id)
        assert (txn.amount, txn.category, txn.account_hint) == (
            Decimal("38.50"), "餐饮", "1234",
        )

    def test_it_goes_through_the_gateway(self, pg_session, user_id):
        """agent 提的写必须过网关(06 §6.6),否则这笔钱哪来的没人答得上来。"""
        an_event(pg_session, user_id)
        run(pg_session, user_id, [judged("t-1")])

        names = [
            row.tool_name
            for row in pg_session.execute(
                text("SELECT tool_name FROM tool_calls WHERE user_id = :u ORDER BY id"),
                {"u": user_id},
            ).all()
        ]
        assert names == ["llm.chat", "txn.record"]

    def test_the_amount_comes_from_the_regex_not_the_model(self, pg_session, user_id):
        """铁律 9。模型这一轮只回答了"这是哪一类资金变动"。"""
        event_id = an_event(pg_session, user_id, amount="38.50")

        # 模型顺手报了个金额 —— 就算它报了,也一个字都不该被采用
        run(pg_session, user_id, [judged("t-1", amount="9999.00")])

        txn = transactions.get_by_event(user_id, pg_session, source_event_id=event_id)
        assert txn.amount == Decimal("38.50")

    def test_a_repayment_is_recorded_but_is_not_spending(self, pg_session, user_id):
        event_id = an_event(pg_session, user_id)
        run(pg_session, user_id, [judged("t-1", judgment="repayment", category=None)])

        txn = transactions.get_by_event(user_id, pg_session, source_event_id=event_id)
        assert txn.counts_as_spending is False
        assert txn.category is None  # 只有支出需要分类

    def test_a_cross_channel_duplicate_is_merged_not_counted_twice(self, pg_session, user_id):
        """支付宝和银行短信各来一条。**recorded 只能是 1** ——
        算成 2 的话对账时你会以为账本丢了一笔。"""
        an_event(pg_session, user_id, external_id="bank-1")
        an_event(
            pg_session, user_id, external_id="alipay-1", channel="alipay",
            account_hint=None, occurred_at=WINDOW_TIME + timedelta(minutes=1),
        )

        results, _ = run(pg_session, user_id, [judged("bank-1"), judged("alipay-1")])

        assert sum(r.recorded for r in results) == 1
        assert sum(r.merged for r in results) == 1
        count = pg_session.execute(
            text("SELECT count(*) FROM transactions WHERE user_id = :u"), {"u": user_id}
        ).scalar_one()
        assert count == 1


class TestWhatDoesNotLandInTheLedger:
    def test_marketing_is_discarded_and_the_queue_stays_empty(self, pg_session, user_id):
        """**营销不进队列。** 它们每天都有,淹掉的队列等于没有队列。"""
        an_event(pg_session, user_id)

        results, _ = run(
            pg_session, user_id, [judged("t-1", judgment="marketing", category=None)]
        )

        assert sum(r.discarded for r in results) == 1
        assert pending.count_pending(user_id, pg_session, now=NOW) == 0
        assert _ledger_size(pg_session, user_id) == 0

    def test_low_confidence_goes_to_the_queue_in_a_recordable_shape(self, pg_session, user_id):
        """payload 是**照它就能入账的形状**,不是给人看的描述。"""
        event_id = an_event(pg_session, user_id)

        results, _ = run(pg_session, user_id, [judged("t-1", confidence=0.4)])

        assert sum(r.queued for r in results) == 1
        assert _ledger_size(pg_session, user_id) == 0

        (item,) = pending.list_pending(user_id, pg_session, now=NOW)
        assert item.kind is pending.PendingKind.TRANSACTION
        assert item.target_table == "transactions"
        assert item.source_event_id == event_id
        assert item.payload["amount"] == "38.50"
        assert item.payload["category"] == "餐饮"  # 给个具体分类才好改

    def test_a_failed_review_is_queued_with_that_reason(self, pg_session, user_id):
        """第 4 层拦下来的和"模型没把握"要分得开 —— 前者是我们的问题。"""
        an_event(pg_session, user_id)

        results, _ = run(pg_session, user_id, [judged("t-1", category="外卖")])

        assert sum(r.failed_review for r in results) == 1
        (item,) = pending.list_pending(user_id, pg_session, now=NOW)
        assert item.reason is pending.PendingReason.CHECK_FAILED

    def test_a_judgment_pointing_at_nothing_records_nothing(self, pg_session, user_id):
        an_event(pg_session, user_id)

        results, _ = run(pg_session, user_id, [judged("根本没这条")])

        assert sum(r.dropped_ungrounded for r in results) == 1
        assert _ledger_size(pg_session, user_id) == 0


class TestCategorization:
    def test_a_learned_rule_is_used_next_time(self, pg_session, user_id):
        """ADR-008 的两级策略,端到端那一遍。"""
        an_event(pg_session, user_id, external_id="t-1")
        run(pg_session, user_id, [judged("t-1")])

        (rule,) = merchant_rules.list_rules(user_id, pg_session)
        assert (rule.pattern, rule.category) == ("星巴克", "餐饮")

    def test_an_intermediary_never_becomes_a_rule(self, pg_session, user_id):
        """**实时这一遍多数会被挡住,那是常态。** 真正的沉淀要等第 6 片
        月度账单回填出真商户之后。"""
        an_event(pg_session, user_id, merchant="财付通")
        results, _ = run(pg_session, user_id, [judged("t-1")])

        assert merchant_rules.list_rules(user_id, pg_session) == []
        assert sum(r.rules_learned for r in results) == 0
        assert _ledger_size(pg_session, user_id) == 1  # 但账还是记了

    def test_the_rule_wins_over_the_model(self, pg_session, user_id):
        merchant_rules.remember(user_id, pg_session, merchant="星巴克", category="购物")
        event_id = an_event(pg_session, user_id)

        results, _ = run(pg_session, user_id, [judged("t-1", category="餐饮")])

        txn = transactions.get_by_event(user_id, pg_session, source_event_id=event_id)
        assert txn.category == "购物"
        assert sum(r.categorized_by_rule for r in results) == 1
        assert sum(r.categorized_by_llm for r in results) == 0


class TestTheWindow:
    def test_an_empty_window_does_not_call_the_model(self, pg_session, user_id):
        """铁律 9:没有素材就不该花一次调用。"""
        results, llm = run(pg_session, user_id, [])

        assert all(r.no_events for r in results)
        assert llm.calls == 0

    def test_a_window_is_not_processed_twice(self, pg_session, user_id):
        an_event(pg_session, user_id)
        run(pg_session, user_id, [judged("t-1")])

        again, llm = run(pg_session, user_id, [judged("t-1")])

        assert again == []  # 窗口全被认领过了,一个都没跑
        assert llm.calls == 0
        assert _ledger_size(pg_session, user_id) == 1

    def test_a_model_failure_is_alerted_not_swallowed(self, pg_session, user_id):
        an_event(pg_session, user_id)
        alerter = RecordingAlerter()

        llm = FakeLLM([])
        llm.chat = lambda messages: _Response('"不是对象"')  # noqa: ARG005
        bookkeeping.run_once(
            user_id,
            pg_session,
            deps=BookkeepingDeps(llm=llm, alerter=alerter),
            now=NOW,
        )

        assert alerter.sent, "判定失败必须告警,不能静默跳过"
        assert _ledger_size(pg_session, user_id) == 0

    def test_the_run_is_recorded_with_its_numbers(self, pg_session, user_id):
        """job_runs 里那行是"这个窗口到底发生了什么"唯一的记录。"""
        an_event(pg_session, user_id)
        run(pg_session, user_id, [judged("t-1")])

        row = pg_session.execute(
            text(
                "SELECT status, stats FROM job_runs"
                " WHERE user_id = :u AND job_name = 'bookkeeping'"
                " ORDER BY window_start DESC LIMIT 1"
            ),
            {"u": user_id},
        ).one()
        assert row.status == "succeeded"
        assert row.stats["recorded"] == 1
        assert row.stats["llm_share"] == 1.0  # 第一笔当然全靠模型


def _ledger_size(session, user_id) -> int:
    return session.execute(
        text("SELECT count(*) FROM transactions WHERE user_id = :u"), {"u": user_id}
    ).scalar_one()
