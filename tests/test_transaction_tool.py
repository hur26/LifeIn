"""入账的 L2 工具与 `undo_record()`(P2 第 5 片的后半)。需要真实 PostgreSQL。

两件事在这里验:

1. **过网关**。06 §6.6 的判据是"谁提的",记账 job 是 agent 提的,所以必须过。
   过网关的意义全在 `tool_calls` 那条记录上 —— 它是"这笔钱哪来的、怎么撤"
   唯一的答案,绕过网关也写得进去,但写出来的东西没人能解释。
2. **撤得掉**。L2 的判据是可回滚,而回滚信息指向的东西必须真的存在。
   `record()` 有三种落地方式(新建、合并、重复上报),`undo_record()`
   三种都要认 —— 让调用方自己分辨的话,判断错的那次要么删掉别人的交易,
   要么留下一笔撤不掉的。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lifein.governance.gateway import CallContext, Gateway
from lifein.governance.registry import ToolLevel, registered_tools
from lifein.models.normalized import Trust
from lifein.repos import transactions
from lifein.repos.tool_calls import PostgresAuditSink
from lifein.repos.transactions import TxnKind

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 20, 15, tzinfo=UTC)


@pytest.fixture(autouse=True)
def tools_registered():
    from lifein.bootstrap import register_tools

    register_tools()


def an_event(session, user_id, *, external_id: str = "t-1") -> int:
    from sqlalchemy import text

    return session.execute(
        text(
            "INSERT INTO raw_events (user_id, source, external_id, occurred_at, trust,"
            " raw) VALUES (:u, 'notification', :x, :t, 'external', '{}'::jsonb)"
            " RETURNING id"
        ),
        {"u": user_id, "x": external_id, "t": NOW},
    ).scalar_one()


def call(session, user_id, name: str, args: dict, *, event_id: int):
    gateway = Gateway(PostgresAuditSink(user_id, session))
    ctx = CallContext(
        user_id=user_id,
        agent="bookkeeper",
        trust=Trust.EXTERNAL,
        source_event_id=event_id,
        session=session,
    )
    return gateway.call(ctx, name, args)


def txn_args(event_id: int, **overrides) -> dict:
    return {
        "occurred_at": NOW.isoformat(),
        "amount": "38.50",
        "direction": "debit",
        "kind": "expense",
        "channel": "bank_sms",
        "source_event_id": event_id,
        "confidence": 0.95,
        "category": "餐饮",
        "account_hint": "1234",
        **overrides,
    }


class TestTheContract:
    def test_it_is_l2_and_returns_rollback(self):
        """L2 不返回回滚信息就该当成错误 —— 副作用已经发生却收不回来。"""
        spec = registered_tools()["txn.record"]
        assert spec.level is ToolLevel.L2
        assert spec.returns_rollback is True

    def test_undo_is_deliberately_not_a_tool(self):
        """**撤销没有做成工具,这是故意的。**

        入账是 agent 提的所以过网关;而"这笔记错了"是用户在 App 里点的,
        按 06 §6.6 那张表本来就不过网关。注册成工具的话它会是一个谁都不能调
        的工具,而那种东西和没做完的区别只有一个:它看起来像做完了。
        """
        assert "txn.undo" not in registered_tools()


class TestRecording:
    def test_a_transaction_lands_and_is_audited(self, pg_session, user_id):
        event_id = an_event(pg_session, user_id)

        outcome = call(pg_session, user_id, "txn.record", txn_args(event_id), event_id=event_id)

        assert outcome["created"] is True
        txn = transactions.get_by_event(user_id, pg_session, source_event_id=event_id)
        assert (txn.amount, txn.category, txn.kind) == (Decimal("38.50"), "餐饮", TxnKind.EXPENSE)

    def test_the_audit_row_says_how_to_undo_it(self, pg_session, user_id):
        """**这条记录是"这笔钱哪来的、怎么撤"唯一的答案。**"""
        from sqlalchemy import text

        event_id = an_event(pg_session, user_id)
        call(pg_session, user_id, "txn.record", txn_args(event_id), event_id=event_id)

        row = pg_session.execute(
            text(
                "SELECT agent, tool_name, level, rollback_info, llm_fields_sent"
                "  FROM tool_calls WHERE user_id = :u ORDER BY id DESC LIMIT 1"
            ),
            {"u": user_id},
        ).one()
        assert (row.agent, row.tool_name, row.level) == ("bookkeeper", "txn.record", "L2")
        # tool_calls 没有 source_event_id 这一列,来源靠 rollback_info 里那个事件 id
        # 追回去(铁律 5)—— 撤销要用的和追溯要用的本来就是同一个东西
        assert row.rollback_info["undo_transaction_for_event"] == event_id
        assert list(row.llm_fields_sent) == []  # 入账这一步不过模型

    def test_a_category_outside_the_enum_is_refused(self, pg_session, user_id):
        """第三道枚举检查。三处都挡,是因为三条路都能走到入账,
        而报表长草只需要一条漏网。"""
        event_id = an_event(pg_session, user_id)

        with pytest.raises(Exception):  # noqa: B017 - 网关把校验错误包起来
            call(
                pg_session, user_id, "txn.record",
                txn_args(event_id, category="外卖"), event_id=event_id,
            )
        assert transactions.get_by_event(user_id, pg_session, source_event_id=event_id) is None

    def test_a_negative_amount_is_refused(self, pg_session, user_id):
        """正负由 direction 表达。混着来的话退款和支出在求和时会互相抵消。"""
        event_id = an_event(pg_session, user_id)

        with pytest.raises(Exception):  # noqa: B017
            call(
                pg_session, user_id, "txn.record",
                txn_args(event_id, amount="-38.50"), event_id=event_id,
            )

    def test_a_naive_timestamp_is_refused(self, pg_session, user_id):
        """无时区的时间会让一笔深夜的消费落到前一天,而月度报表按天切。"""
        event_id = an_event(pg_session, user_id)

        with pytest.raises(Exception):  # noqa: B017
            call(
                pg_session, user_id, "txn.record",
                txn_args(event_id, occurred_at="2026-09-08T20:15:00"), event_id=event_id,
            )


class TestUndo:
    """`record()` 三种落地方式,撤销三种都要认。"""

    def test_a_created_row_is_deleted(self, pg_session, user_id):
        """删而不是标记作废:这一行完全是派生的,源事件还在,重跑会长回来。"""
        event_id = an_event(pg_session, user_id)
        call(pg_session, user_id, "txn.record", txn_args(event_id), event_id=event_id)

        assert transactions.undo_record(user_id, pg_session, source_event_id=event_id) is True
        assert transactions.get_by_event(user_id, pg_session, source_event_id=event_id) is None

    def test_a_merged_event_is_only_detached(self, pg_session, user_id):
        """**并进去的那笔不删** —— 它是另一条通知记下的,和这次撤销无关。"""
        first = an_event(pg_session, user_id, external_id="bank-1")
        second = an_event(pg_session, user_id, external_id="alipay-1")

        call(pg_session, user_id, "txn.record", txn_args(first), event_id=first)
        merged = call(
            pg_session, user_id, "txn.record",
            txn_args(second, channel="alipay", account_hint=None,
                     occurred_at=(NOW + timedelta(minutes=1)).isoformat()),
            event_id=second,
        )
        assert merged["merged_into"] is not None

        assert transactions.undo_record(user_id, pg_session, source_event_id=second) is True

        # 银行那一笔还在,只是不再声称自己包含支付宝那条事件
        kept = transactions.get_by_event(user_id, pg_session, source_event_id=first)
        assert kept is not None
        assert second not in kept.merged_from_event_ids

    def test_undoing_twice_is_not_an_error(self, pg_session, user_id):
        """回滚经常是重试的一部分,第二次撤同一条不该炸。"""
        event_id = an_event(pg_session, user_id)
        call(pg_session, user_id, "txn.record", txn_args(event_id), event_id=event_id)

        assert transactions.undo_record(user_id, pg_session, source_event_id=event_id) is True
        assert transactions.undo_record(user_id, pg_session, source_event_id=event_id) is False

    def test_undoing_something_that_was_never_recorded_is_false(self, pg_session, user_id):
        event_id = an_event(pg_session, user_id)
        assert transactions.undo_record(user_id, pg_session, source_event_id=event_id) is False

    def test_undo_does_not_reach_across_users(self, pg_session, user_id):
        """铁律 1。撤销带的是 user_id,不是只带一个自增主键。"""
        event_id = an_event(pg_session, user_id)
        call(pg_session, user_id, "txn.record", txn_args(event_id), event_id=event_id)

        other = "99999999-9999-9999-9999-999999999999"
        assert transactions.undo_record(other, pg_session, source_event_id=event_id) is False
        assert transactions.get_by_event(user_id, pg_session, source_event_id=event_id) is not None


def test_recording_the_same_event_twice_is_a_duplicate_not_a_second_row(pg_session, user_id):
    """采集器重试。库上的唯一键挡的就是它。"""
    event_id = an_event(pg_session, user_id)

    first = call(pg_session, user_id, "txn.record", txn_args(event_id), event_id=event_id)
    second = call(pg_session, user_id, "txn.record", txn_args(event_id), event_id=event_id)

    assert first["created"] is True
    assert second["duplicate"] is True
    assert second["transaction_id"] == first["transaction_id"]
