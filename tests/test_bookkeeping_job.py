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
import re
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


class EchoLLM:
    """按**这一批实际收到的素材**回答,不是每次都回同一份。

    分批那件事必须这样测:一个"每次都返回同样 items"的假模型,在只送了
    前 40 条的旧代码下也照样能让 45 笔全部入账 —— 那样测出来的绿是假的。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.batch_sizes: list[int] = []

    def chat(self, messages):
        self.calls += 1
        refs = re.findall(r'id="([^"]+)"', messages[-1]["content"])
        self.batch_sizes.append(len(refs))
        return _Response(
            json.dumps({"items": [judged(ref) for ref in refs]}, ensure_ascii=False)
        )


class BrokenGateway:
    """入账这一步炸掉。用来验"整窗算失败",而不是"悄悄跳过这一笔"。"""

    def call(self, ctx, tool, args):
        raise RuntimeError("数据库连接断了")


class TestABusyDay:
    """一天超过 40 条交易。**这是原来会静默丢账的那条路。**

    `judge` 内部 `[:MAX_EVENTS]` 只取最新的 40 条,而 job 把整窗事件一次性
    交给它 —— 较旧的那些既没入账也没进待确认,窗口却照样 succeeded,
    于是它们再也不会被读第二次。表现是"这天少了几笔",而那要到月底才看得出来。
    """

    def events(self, session, user_id, count: int) -> list[str]:
        """建 count 条交易事件,返回它们的 external_id(按时间从新到旧)。

        时间和金额都各不相同:排序稳定,才说得清"最旧的那条"是哪条,
        而金额不同才不会被跨渠道合并顺手并掉。
        """
        refs = []
        for i in range(count):
            ref = f"t-{i:03d}"
            an_event(
                session,
                user_id,
                external_id=ref,
                amount=f"{10 + i}.00",
                occurred_at=WINDOW_TIME - timedelta(minutes=i),
            )
            refs.append(ref)
        return refs

    def test_forty_five_events_take_two_model_calls(self, pg_session, user_id):
        self.events(pg_session, user_id, 45)
        llm = EchoLLM()
        bookkeeping.run_once(
            user_id, pg_session,
            deps=BookkeepingDeps(llm=llm, alerter=RecordingAlerter()), now=NOW,
        )
        assert llm.calls == 2
        assert sorted(llm.batch_sizes) == [5, 40]

    def test_the_oldest_events_still_get_booked(self, pg_session, user_id):
        """**这一条就是那个 bug 的回归测试。**

        断言的不是"总数对得上"(那个数在旧代码下也可能凑巧对),
        而是**最旧的那一条在不在账本里** —— 它正是被截断掉的那一头。
        """
        refs = self.events(pg_session, user_id, 45)
        bookkeeping.run_once(
            user_id, pg_session,
            deps=BookkeepingDeps(llm=EchoLLM(), alerter=RecordingAlerter()), now=NOW,
        )
        assert _ledger_size(pg_session, user_id) == 45
        oldest = refs[-1]  # occurred_at 最早的那条
        got = pg_session.execute(
            text(
                "SELECT count(*) FROM transactions t JOIN raw_events e"
                " ON e.id = t.source_event_id"
                " WHERE t.user_id = :u AND e.external_id = :x"
            ),
            {"u": user_id, "x": oldest},
        ).scalar_one()
        assert got == 1, "最旧的那笔被截断掉了 —— 这正是原来的行为"

    def test_reading_more_than_the_window_limit_alerts(self, pg_session, user_id, monkeypatch):
        """真读不完的时候要喊一声。**静默截断才是问题**,截断本身不是。"""
        monkeypatch.setattr(bookkeeping, "MAX_EVENTS_PER_WINDOW", 3)
        self.events(pg_session, user_id, 5)
        alerter = RecordingAlerter()
        bookkeeping.run_once(
            user_id, pg_session,
            deps=BookkeepingDeps(llm=EchoLLM(), alerter=alerter), now=NOW,
        )
        assert any("上限" in body for _, body in alerter.sent)


class TestARecordThatBlowsUp:
    """`txn.record` 抛异常。原来的注释说"这一笔明天还会被判一次" ——
    而窗口标成 succeeded 之后不会有明天。"""

    def test_the_whole_window_is_marked_failed(self, pg_session, user_id):
        an_event(pg_session, user_id)
        alerter = RecordingAlerter()
        bookkeeping.run_once(
            user_id, pg_session,
            deps=BookkeepingDeps(
                llm=FakeLLM([judged("t-1")]), alerter=alerter,
                gateway_factory=lambda u, s: BrokenGateway(),
            ),
            now=NOW,
        )
        status = pg_session.execute(
            text(
                "SELECT status FROM job_runs WHERE user_id = :u"
                " AND job_name = 'bookkeeping'"
            ),
            {"u": user_id},
        ).scalar_one()
        assert status == "failed"
        assert alerter.sent

    def test_the_retry_books_it_and_does_not_double_book(self, pg_session, user_id):
        """**两个改动接起来的那一条。**

        第一遍入账炸了 → 整窗 failed;第二遍窗口被重新认领 → 这次记上了。
        跑第三遍不会记第二笔:靠的是 `UNIQUE (user_id, source_event_id)`,
        不是靠这个 job 记得自己干过什么。
        """
        an_event(pg_session, user_id)
        broken = BookkeepingDeps(
            llm=FakeLLM([judged("t-1")]), alerter=RecordingAlerter(),
            gateway_factory=lambda u, s: BrokenGateway(),
        )
        bookkeeping.run_once(user_id, pg_session, deps=broken, now=NOW)
        assert _ledger_size(pg_session, user_id) == 0

        ok = BookkeepingDeps(llm=FakeLLM([judged("t-1")]), alerter=RecordingAlerter())
        [second] = [
            r for r in bookkeeping.run_once(user_id, pg_session, deps=ok, now=NOW)
            if not r.skipped
        ]
        assert second.recorded == 1
        assert _ledger_size(pg_session, user_id) == 1

        third = bookkeeping.run_once(user_id, pg_session, deps=ok, now=NOW)
        assert all(r.skipped for r in third)
        assert _ledger_size(pg_session, user_id) == 1


def _ledger_size(session, user_id) -> int:
    return session.execute(
        text("SELECT count(*) FROM transactions WHERE user_id = :u"), {"u": user_id}
    ).scalar_one()
