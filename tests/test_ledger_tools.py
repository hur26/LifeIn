"""账本的只读工具(P2 收尾:03 那条"在企微里对话式完成"的查询那一半)。

需要真实 PostgreSQL:查的全是库。

这一组盯三件事:

1. **金额原样是字符串**。让模型格式化数字,它会顺手把 38.50 写成 38.5 或 39,
   而那两个都不是账本上的数
2. **合计由工具算,不由模型算**(铁律 9)。模型求和会错,
   而错了的那个数看起来和对的一模一样
3. **它是 L1,只读**。查账本错了顶多答得不对;而改错一笔账是另一个量级
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from lifein.governance.gateway import CallContext, Gateway
from lifein.governance.registry import ToolLevel, registered_tools
from lifein.models.normalized import Trust
from lifein.repos import budgets, transactions
from lifein.repos.tool_calls import PostgresAuditSink
from lifein.repos.transactions import Direction, TxnKind

pytestmark = pytest.mark.integration

SHANGHAI = timezone(timedelta(hours=8))
IN_AUGUST = datetime(2026, 8, 20, 12, 0, tzinfo=SHANGHAI)


def a_txn(
    session,
    user_id,
    *,
    amount: str = "38.50",
    category: str | None = "餐饮",
    merchant: str | None = "星巴克",
    kind: TxnKind = TxnKind.EXPENSE,
    when: datetime = IN_AUGUST,
    external_id: str | None = None,
):
    event_id = session.execute(
        text(
            "INSERT INTO raw_events (user_id, source, external_id, occurred_at, trust, raw)"
            " VALUES (:u, 'notification', :e, :t, 'external', '{}'::jsonb) RETURNING id"
        ),
        {"u": user_id, "e": external_id or f"{amount}-{merchant}-{when.isoformat()}", "t": when},
    ).scalar_one()
    return transactions.record(
        user_id,
        session,
        occurred_at=when,
        amount=Decimal(amount),
        direction=Direction.DEBIT,
        kind=kind,
        channel="bank_sms",
        source_event_id=event_id,
        confidence=1.0,
        category=category if kind is TxnKind.EXPENSE else None,
        merchant_raw=merchant,
    ).transaction


def call(session, user_id, name: str, args: dict, *, agent: str = "qa"):
    gateway = Gateway(PostgresAuditSink(user_id, session))
    ctx = CallContext(
        user_id=user_id, agent=agent, trust=Trust.USER_INPUT, source_event_id=None, session=session
    )
    return gateway.call(ctx, name, args)


class TestTheContract:
    def test_both_are_l1(self):
        """只读。查账本错了顶多答得不对,而改错一笔账是另一个量级。"""
        for name in ("ledger.query", "ledger.spending"):
            assert registered_tools()[name].level is ToolLevel.L1

    def test_only_the_qa_agent_may_read_the_ledger(self, pg_session, user_id):
        """**网关那道"双重门"。** 问答能查账本,不代表别的 agent 也能 ——
        它读的是这个系统里最私密的一张表。"""
        from lifein.governance.gateway import Denied

        with pytest.raises(Denied):
            call(pg_session, user_id, "ledger.query", {}, agent="daily_digest")

    def test_there_is_no_write_tool(self):
        """**对话式的改账不做成工具。** "把昨天星巴克那笔改成餐饮"要先理解
        指的是哪一笔,而理解错了就是改错一笔账 —— 那条路走待确认,多一次点头。"""
        assert not any(
            name.startswith("ledger.") and name not in ("ledger.query", "ledger.spending")
            for name in registered_tools()
        )


class TestQuerying:
    def test_amounts_stay_strings(self, pg_session, user_id):
        """让模型格式化数字,它会顺手把 38.50 写成 38.5 或者 39。"""
        a_txn(pg_session, user_id, amount="38.50", when=datetime.now(SHANGHAI))

        result = call(pg_session, user_id, "ledger.query", {})
        assert result["transactions"][0]["amount"] == "38.50"

    def test_filtering_by_category(self, pg_session, user_id):
        now = datetime.now(SHANGHAI)
        a_txn(pg_session, user_id, category="餐饮", when=now, external_id="a")
        a_txn(pg_session, user_id, category="交通", when=now, external_id="b")

        result = call(pg_session, user_id, "ledger.query", {"category": "交通"})
        assert [t["category"] for t in result["transactions"]] == ["交通"]

    def test_a_category_outside_the_enum_is_refused(self, pg_session, user_id):
        """**枚举外的类目查出来永远是空**,而空结果会被模型解释成
        "你没花过这类钱" —— 那是一句错的话,而且听起来很确定。"""
        with pytest.raises(Exception):  # noqa: B017 - 网关把校验错误包起来
            call(pg_session, user_id, "ledger.query", {"category": "外卖"})

    def test_the_keyword_searches_merchants(self, pg_session, user_id):
        now = datetime.now(SHANGHAI)
        a_txn(pg_session, user_id, merchant="星巴克", when=now, external_id="a")
        a_txn(pg_session, user_id, merchant="全家", when=now, external_id="b")

        result = call(pg_session, user_id, "ledger.query", {"keyword": "星巴克"})
        assert result["count"] == 1

    def test_a_naive_timestamp_is_refused(self, pg_session, user_id):
        with pytest.raises(Exception):  # noqa: B017
            call(pg_session, user_id, "ledger.query", {"since": "2026-08-01T00:00:00"})

    def test_the_row_cap_is_enforced(self, pg_session, user_id):
        """**五十笔已经超过一条消息该有的长度。** 更多只会让模型去总结,
        而总结意味着它要算钱。"""
        with pytest.raises(Exception):  # noqa: B017
            call(pg_session, user_id, "ledger.query", {"limit": 500})


class TestSpending:
    def test_the_tool_does_the_arithmetic(self, pg_session, user_id):
        """铁律 9。**模型求和会错,而错了的那个数看起来和对的一模一样。**"""
        for i, amount in enumerate(["100.00", "200.50", "38.50"]):
            a_txn(pg_session, user_id, amount=amount, when=IN_AUGUST, external_id=f"e{i}")

        result = call(pg_session, user_id, "ledger.spending", {"period": "2026-08"})

        assert result["total"] == "339.00"
        assert result["count"] == 3

    def test_repayments_do_not_count(self, pg_session, user_id):
        a_txn(pg_session, user_id, amount="100.00", external_id="a")
        a_txn(pg_session, user_id, amount="5000.00", kind=TxnKind.REPAYMENT, external_id="b")

        result = call(pg_session, user_id, "ledger.spending", {"period": "2026-08"})
        assert result["total"] == "100.00"

    def test_budgets_come_along(self, pg_session, user_id):
        """问"这个月花超了没有"时,光有花了多少答不上来。"""
        budgets.set_budget(user_id, pg_session, amount=Decimal("100"), category="餐饮")
        a_txn(pg_session, user_id, amount="150.00")

        result = call(pg_session, user_id, "ledger.spending", {"period": "2026-08"})

        (budget,) = result["budgets"]
        assert (budget["category"], budget["over"]) == ("餐饮", True)

    def test_a_malformed_period_is_refused(self, pg_session, user_id):
        with pytest.raises(Exception):  # noqa: B017
            call(pg_session, user_id, "ledger.spending", {"period": "去年八月"})

    def test_it_does_not_reach_across_users(self, pg_session, user_id):
        """铁律 1。账本比记忆还私密。"""
        a_txn(pg_session, user_id, amount="9999.00")
        other = "99999999-9999-9999-9999-999999999999"

        result = call(pg_session, other, "ledger.spending", {"period": "2026-08"})
        assert result["total"] == "0"
