"""审批的回复入口(P3 第 6 片)。需要真实 PostgreSQL。

**这一片存在的全部理由是"一次只认一次"。** 企微会对非 200 重投,手机点了
手表也可能点 —— 而重投两次点两次同意必须只生效一次。少了它,03 那条
"零重复执行"就从"发两条消息"变成"批准两次",而后者更难发现。

另外两组:

- **认得准**:"同意 12" 认,"同意,但改成三点" 不认 ——
  猜错的代价是发出一条你没同意的消息
- **不泄露**:别人的审批 id 回什么都是"没有这一条",和 06 §6.13 那条 404 同理
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lifein.jobs import approval_reply
from lifein.models.normalized import Trust
from lifein.repos import approvals
from lifein.repos.approvals import ApprovalStatus

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def a_pending(session, user_id, *, body: str = "明天三点见", **overrides):
    payload = {
        "agent": "qa",
        "tool_name": "message.send",
        "tool_args": {"text": body},
        "preview_text": f"替你发一条消息:{body}",
        "trust": Trust.USER_INPUT,
        "now": NOW,
    }
    payload.update(overrides)
    return approvals.enqueue(user_id, session, **payload)


def reply(session, user_id, text: str, *, now: datetime = NOW):
    return approval_reply.handle(session, user_id=user_id, text=text, now=now)


class TestOnlyOnce:
    """**这一片存在的全部理由。**"""

    def test_approving_twice_only_counts_once(self, pg_session, user_id):
        """企微重投、手机和手表各点一次 —— 都会走到这里。"""
        item = a_pending(pg_session, user_id)

        first = reply(pg_session, user_id, f"同意 {item.id}")
        second = reply(pg_session, user_id, f"同意 {item.id}")

        assert first.action == "approved"
        assert second.action is None
        assert "已经处理过" in second.card.title
        assert approvals.get(
            user_id, pg_session, approval_id=item.id
        ).status is ApprovalStatus.APPROVED

    def test_rejecting_after_approving_does_nothing(self, pg_session, user_id):
        """改主意要在点之前 —— 点完再说"算了"改不动,而**这一点要说出来**,
        否则用户会以为拒绝生效了。"""
        item = a_pending(pg_session, user_id)
        reply(pg_session, user_id, f"同意 {item.id}")

        after = reply(pg_session, user_id, f"拒绝 {item.id}")
        assert "已经同意过了" in after.card.summary
        assert approvals.get(
            user_id, pg_session, approval_id=item.id
        ).status is ApprovalStatus.APPROVED

    def test_approving_does_not_execute(self, pg_session, user_id):
        """**回调里不执行。** 回调有超时,而超时重投会再执行一次。
        这里只把状态改成 approved,做的事交给 job。"""
        item = a_pending(pg_session, user_id)
        reply(pg_session, user_id, f"同意 {item.id}")

        after = approvals.get(user_id, pg_session, approval_id=item.id)
        assert after.status is ApprovalStatus.APPROVED
        assert after.executed_at is None

    def test_an_expired_one_says_so(self, pg_session, user_id):
        """一条昨天的审批今天点同意 —— 发出去的内容早就不合时宜了。"""
        item = a_pending(pg_session, user_id, ttl=timedelta(hours=1))

        after = reply(pg_session, user_id, f"同意 {item.id}", now=NOW + timedelta(hours=2))
        assert "过期" in after.card.summary


class TestRecognizing:
    @pytest.mark.parametrize(
        "text", ["同意 {id}", "同意{id}", "同意 #{id}", "批准 {id}", "ok {id}", "确认 {id}"]
    )
    def test_shapes_that_mean_yes(self, pg_session, user_id, text):
        item = a_pending(pg_session, user_id)
        assert reply(pg_session, user_id, text.format(id=item.id)).action == "approved"

    @pytest.mark.parametrize("text", ["拒绝 {id}", "不用 {id}", "算了 {id}", "no {id}"])
    def test_shapes_that_mean_no(self, pg_session, user_id, text):
        item = a_pending(pg_session, user_id)
        assert reply(pg_session, user_id, text.format(id=item.id)).action == "rejected"

    def test_a_qualified_yes_is_not_a_yes(self, pg_session, user_id):
        """**"同意,但改成三点" 不认。** 那要么是想改内容,要么是在跟别人说话,
        而猜错的代价是发出一条你没同意的消息。交给问答去回。"""
        item = a_pending(pg_session, user_id)

        after = reply(pg_session, user_id, f"同意 {item.id},但改成三点")
        assert after.handled is False
        assert approvals.get(
            user_id, pg_session, approval_id=item.id
        ).status is ApprovalStatus.PENDING

    def test_an_ordinary_question_falls_through_to_qa(self, pg_session, user_id):
        assert reply(pg_session, user_id, "这个月餐饮花了多少").handled is False
        assert reply(pg_session, user_id, "").handled is False

    def test_listing_the_queue(self, pg_session, user_id):
        """**每条前面带 id**,因为回复要用它。"""
        first = a_pending(pg_session, user_id, body="给老王回个话", idempotency_key="a")
        a_pending(pg_session, user_id, body="给老李回个话", idempotency_key="b")

        card = reply(pg_session, user_id, "审批").card
        lines = card.sections[0].lines

        assert len(lines) == 2
        assert lines[0].startswith(f"#{first.id} ")
        assert "同意 编号" in card.summary  # 告诉人怎么用

    def test_an_empty_queue_says_so(self, pg_session, user_id):
        assert "没有等你批" in reply(pg_session, user_id, "审批").card.title


class TestNotLeaking:
    def test_someone_elses_approval_looks_the_same_as_a_missing_one(
        self, pg_session, user_id
    ):
        """**不区分"不存在"和"不是你的"** —— 区分等于告诉对方这个 id 存在。"""
        from sqlalchemy import text as sql

        other = "99999999-9999-9999-9999-999999999999"
        pg_session.execute(
            sql(
                "INSERT INTO users (id, display_name, wecom_userid)"
                " VALUES (:i, '别人', 'other')"
            ),
            {"i": other},
        )
        theirs = a_pending(pg_session, other)

        mine = reply(pg_session, user_id, f"同意 {theirs.id}")
        missing = reply(pg_session, user_id, "同意 999999")

        assert mine.card.title == missing.card.title
        assert approvals.get(other, pg_session, approval_id=theirs.id).status is (
            ApprovalStatus.PENDING
        )

    def test_the_queue_only_shows_your_own(self, pg_session, user_id):
        from sqlalchemy import text as sql

        other = "99999999-9999-9999-9999-999999999999"
        pg_session.execute(
            sql(
                "INSERT INTO users (id, display_name, wecom_userid)"
                " VALUES (:i, '别人', 'other2')"
            ),
            {"i": other},
        )
        a_pending(pg_session, other)

        assert "没有等你批" in reply(pg_session, user_id, "审批").card.title
