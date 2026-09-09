"""审批执行 job(P3 第 5、7 片)。需要真实 PostgreSQL。

**这一组测的是 03 的退出条件本身**:"出现任何一次越权或重复执行 →
停止 L3 上线,回去修网关和幂等"。

所以顺序是:先测不该发生的(没批准的不执行、做过的不再做、schema 对不上不猜),
再测该发生的(批准过的真的发出去了,而且审计里查得到谁批准的)。

**"两个执行者同时拿到同一条"在这里有一条专门的用例。**
它是原来那个设计真正会出事的地方,而顺序跑两遍的用例看不出来:
第二遍时那条已经是 `executed` 了。要复现得让两个执行者**都在还是 `approved`
的时候**读到它 —— 而那正是生产里会发生的事。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from lifein.channels.base import Card, Delivery
from lifein.jobs import approval_execute
from lifein.jobs.approval_execute import ExecuteDeps
from lifein.models.normalized import Trust
from lifein.repos import approvals
from lifein.repos.approvals import ApprovalStatus

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


class Sent:
    name = "weixin"

    def __init__(self) -> None:
        self.cards: list[Card] = []

    def send(self, user_id: str, card: Card) -> Delivery:
        self.cards.append(card)
        return Delivery(channel=self.name, delivery_id=f"d-{len(self.cards)}")


class Broken:
    name = "weixin"

    def send(self, user_id: str, card: Card) -> Delivery:
        raise RuntimeError("企微接口 500")


class Loud:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def alert(self, title: str, body: str) -> None:
        self.sent.append((title, body))


def deps(channel=None, alerter=None, now=NOW):
    return ExecuteDeps(
        channel=channel or Sent(), alerter=alerter or Loud(), now=lambda: now
    )


def a_pending(session, user_id, *, text_body: str = "明天三点见", **overrides):
    payload = {
        "agent": "qa",
        "tool_name": "message.send",
        "tool_args": {"text": text_body, "title": "来自 LifeIn"},
        "preview_text": f"替你发一条消息:{text_body}",
        "trust": Trust.USER_INPUT,
        "now": NOW,
    }
    payload.update(overrides)
    return approvals.enqueue(user_id, session, **payload)


def an_approved(session, user_id, **overrides):
    item = a_pending(session, user_id, **overrides)
    approvals.approve(user_id, session, approval_id=item.id, now=NOW)
    return item


class TestWhatMustNotHappen:
    def test_something_never_approved_is_never_executed(self, pg_session, user_id):
        """队列里躺着一条没点过同意的 —— 它一个字都不该发出去。"""
        a_pending(pg_session, user_id)
        channel = Sent()

        result = approval_execute.run_once(user_id, pg_session, deps=deps(channel))

        assert result.considered == 0
        assert channel.cards == []

    def test_executing_twice_only_sends_once(self, pg_session, user_id):
        """**03 那条"零重复执行"。** 两次跑,一条消息。"""
        an_approved(pg_session, user_id)
        channel = Sent()

        first = approval_execute.run_once(user_id, pg_session, deps=deps(channel))
        second = approval_execute.run_once(user_id, pg_session, deps=deps(channel))

        assert (first.executed, second.executed) == (1, 0)
        assert len(channel.cards) == 1

    def test_two_executors_racing_only_send_once(self, pg_session, user_id):
        """**这一条是那个 bug 的回归测试。**

        上面那条顺序跑两遍的用例在旧代码上也是绿的:第二遍时行已经是
        `executed` 了。真正会出事的是两个执行者**都在还是 `approved` 的时候**
        读到同一条 —— 生产里那就是两个进程、或者一次调度重叠。

        旧代码的两步是"先执行、再写结果",于是两个都发了出去,只是其中一个
        在写结果时才发现自己是第二个。而那时消息已经出去了,告警只能事后说。
        """
        item = an_approved(pg_session, user_id)
        channel = Sent()
        d = deps(channel)

        # 两个执行者各自读到了同一条 approved 的记录
        (a,) = approvals.list_ready(user_id, pg_session)
        (b,) = approvals.list_ready(user_id, pg_session)
        assert a.id == b.id == item.id

        first = approval_execute.ExecuteResult()
        second = approval_execute.ExecuteResult()
        approval_execute._execute_one(user_id, pg_session, a, deps=d, result=first)
        approval_execute._execute_one(user_id, pg_session, b, deps=d, result=second)

        assert len(channel.cards) == 1, "第二个执行者不该把消息也发出去"
        assert (first.executed, second.executed) == (1, 0)
        assert second.skipped == 1

    def test_the_loser_of_the_race_does_not_alert(self, pg_session, user_id):
        """**认领不到不是错误,是"只做一次"在起作用。**

        旧代码在这里会发一条"可能的重复执行"告警 —— 那时它是对的,因为
        确实可能重复发了。现在什么都没发出去,再告警就是在制造噪音,
        而 03 那张验收表上"有没有重复执行"要看的是别的东西。
        """
        item = an_approved(pg_session, user_id)
        alerter = Loud()
        d = deps(Sent(), alerter)

        (a,) = approvals.list_ready(user_id, pg_session)
        (b,) = approvals.list_ready(user_id, pg_session)
        for item_read in (a, b):
            approval_execute._execute_one(
                user_id, pg_session, item_read, deps=d,
                result=approval_execute.ExecuteResult(),
            )

        assert alerter.sent == []
        assert approvals.get(user_id, pg_session, approval_id=item.id).status is (
            ApprovalStatus.EXECUTED
        )

    def test_a_rejected_one_is_never_executed(self, pg_session, user_id):
        item = a_pending(pg_session, user_id)
        approvals.reject(user_id, pg_session, approval_id=item.id, now=NOW)
        channel = Sent()

        approval_execute.run_once(user_id, pg_session, deps=deps(channel))
        assert channel.cards == []

    def test_an_expired_one_is_swept_not_executed(self, pg_session, user_id):
        """**过期是安全属性。** 一条昨天的"帮我回复老王"今天发出去,
        内容早就不合时宜了。"""
        item = a_pending(pg_session, user_id, ttl=timedelta(hours=1))
        channel = Sent()

        result = approval_execute.run_once(
            user_id, pg_session, deps=deps(channel, now=NOW + timedelta(hours=2))
        )

        assert result.expired == 1
        assert channel.cards == []
        after = approvals.get(user_id, pg_session, approval_id=item.id)
        assert after.status is ApprovalStatus.EXPIRED

    def test_a_changed_arg_schema_stops_it(self, pg_session, user_id):
        """入参 schema 改过了,而队列里存的是旧形状。**宁可不做。**"""
        item = an_approved(pg_session, user_id)
        pg_session.execute(
            text("UPDATE approvals SET tool_args = '{\"nope\": 1}'::jsonb WHERE id = :i"),
            {"i": item.id},
        )
        channel, alerter = Sent(), Loud()

        result = approval_execute.run_once(user_id, pg_session, deps=deps(channel, alerter))

        assert (result.failed, channel.cards) == (1, [])
        assert alerter.sent

    def test_a_non_l3_tool_in_the_queue_is_refused(self, pg_session, user_id):
        """队列里出现非 L3 说明有人绕过了网关。**停下来告警,别执行。**"""
        an_approved(pg_session, user_id, tool_name="todo.create", tool_args={"title": "x"})
        alerter = Loud()

        result = approval_execute.run_once(user_id, pg_session, deps=deps(alerter=alerter))

        assert result.failed == 1
        assert any("不是 L3" in body for _, body in alerter.sent)

    def test_a_vanished_tool_does_not_count_as_done(self, pg_session, user_id):
        """工具被改名或删掉了,而队列里还有引用它的审批。"""
        an_approved(pg_session, user_id, tool_name="message.gone")

        result = approval_execute.run_once(user_id, pg_session, deps=deps())
        assert result.failed == 1


class TestWhatMustHappen:
    def test_an_approved_message_really_goes_out(self, pg_session, user_id):
        """**这是 P3 的目标那句话:"你敢让它代你发一条真实消息"。**"""
        item = an_approved(pg_session, user_id, text_body="明天三点见")
        channel = Sent()

        result = approval_execute.run_once(user_id, pg_session, deps=deps(channel))

        assert result.executed == 1
        assert channel.cards[0].summary == "明天三点见"
        after = approvals.get(user_id, pg_session, approval_id=item.id)
        assert after.status is ApprovalStatus.EXECUTED
        assert after.result["channel"] == "weixin"

    def test_what_was_previewed_is_what_gets_sent(self, pg_session, user_id):
        """**点同意才有意义的前提。** 卡片上写的是这段字,发出去的也必须是它 ——
        中间再过一次模型的话,你同意的和发出去的就是两回事了。"""
        item = an_approved(pg_session, user_id, text_body="周五的会改到下午两点")
        channel = Sent()

        approval_execute.run_once(user_id, pg_session, deps=deps(channel))
        assert channel.cards[0].summary in item.preview_text

    def test_the_audit_says_who_approved_it(self, pg_session, user_id):
        """**L3 那条记录是"这件事到底谁批准的"唯一的答案。**

        `rollback_info` 里放的不是"怎么撤" —— 发出去的消息撤不回来,
        承诺一个撤不回来的东西能回滚,比不承诺更糟。
        """
        item = an_approved(pg_session, user_id)
        approval_execute.run_once(user_id, pg_session, deps=deps())

        row = pg_session.execute(
            text(
                "SELECT tool_name, level, rollback_info FROM tool_calls"
                " WHERE user_id = :u ORDER BY id DESC LIMIT 1"
            ),
            {"u": user_id},
        ).one()
        assert (row.tool_name, row.level) == ("message.send", "L3")
        assert row.rollback_info["approval_id"] == item.id
        assert "不可回滚" in row.rollback_info["note"]

    def test_a_send_failure_is_not_marked_done(self, pg_session, user_id):
        """**失败了不自动重试。** 失败的原因可能是"对面已经收到了,只是响应
        超时",而那种情况下重试就是发第二条。"""
        item = an_approved(pg_session, user_id)
        alerter = Loud()

        result = approval_execute.run_once(
            user_id, pg_session, deps=deps(Broken(), alerter)
        )

        assert result.failed == 1
        after = approvals.get(user_id, pg_session, approval_id=item.id)
        assert after.status is ApprovalStatus.FAILED
        assert after.result["error"].startswith("RuntimeError")
        # 它不会自己回到 approved
        assert approvals.list_ready(user_id, pg_session) == []
        assert alerter.sent

    def test_it_does_not_reach_across_users(self, pg_session, user_id):
        """铁律 1。别人批准过的东西,不该由你这一次运行发出去。"""
        an_approved(pg_session, user_id)
        other = "99999999-9999-9999-9999-999999999999"
        channel = Sent()

        result = approval_execute.run_once(other, pg_session, deps=deps(channel))
        assert (result.considered, channel.cards) == (0, [])


def test_the_batch_size_is_small(pg_session, user_id):
    """**一次跑二十条代发消息意味着出错时也是二十条。** P3 的验收标准要的是
    二十次成功的操作,不是二十次并发。"""
    for i in range(5):
        an_approved(pg_session, user_id, text_body=f"消息{i}", idempotency_key=f"k{i}")
    channel = Sent()

    result = approval_execute.run_once(user_id, pg_session, deps=deps(channel), limit=2)

    assert result.executed == 2
    assert len(channel.cards) == 2


class TestStuckInExecuting:
    """认领之后、拿到结果之前进程挂了。**这是新顺序的代价,而它是可见的。**

    含义是"我们开始发了,但不知道发出去没有" —— 唯一不能替用户猜的情况:
    重试可能发第二条,标成失败会让人以为一条都没发。
    """

    def stuck_one(self, pg_session, user_id, *, claimed_at):
        item = an_approved(pg_session, user_id)
        approvals.claim_for_execution(
            user_id, pg_session, approval_id=item.id, now=claimed_at
        )
        return item

    def test_a_fresh_claim_is_not_reported(self, pg_session, user_id):
        """刚认领的不算卡住。**一条会天天误报的告警等于没有告警。**"""
        self.stuck_one(pg_session, user_id, claimed_at=NOW)
        alerter = Loud()

        result = approval_execute.run_once(
            user_id, pg_session, deps=deps(Sent(), alerter, now=NOW)
        )
        assert result.stuck == 0
        assert alerter.sent == []

    def test_an_old_claim_is_alerted(self, pg_session, user_id):
        item = self.stuck_one(pg_session, user_id, claimed_at=NOW - timedelta(hours=1))
        alerter = Loud()

        result = approval_execute.run_once(
            user_id, pg_session, deps=deps(Sent(), alerter, now=NOW)
        )
        assert result.stuck == 1
        assert alerter.sent, "卡住的审批必须告警 —— 它是 03 验收表里那一行的判据"
        assert f"#{item.id}" in alerter.sent[0][1]

    def test_it_is_not_retried(self, pg_session, user_id):
        """**只报,不动。** 重试就是发第二条,而我们不知道第一条发出去没有。"""
        item = self.stuck_one(pg_session, user_id, claimed_at=NOW - timedelta(hours=1))
        channel = Sent()

        approval_execute.run_once(user_id, pg_session, deps=deps(channel, now=NOW))

        assert channel.cards == []
        after = approvals.get(user_id, pg_session, approval_id=item.id)
        assert after.status is ApprovalStatus.EXECUTING, "不该被自动改成 failed"
