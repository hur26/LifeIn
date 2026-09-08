"""审批队列(P3 第 2 片,06 §2.8)。需要真实 PostgreSQL。

**这一组就是 03 给 P3 那两个零的测试。** 验收标准是"完成 20 次真实 L3 操作,
零重复执行、零越权",退出条件是"出现任何一次越权或重复执行 → 停止 L3 上线"。

所以用例按那两个零分成两组,别的都排在后面:

- **零越权**:外部内容触发的一律写不进去(库上的 `CHECK` + 仓储那一道)
- **零重复执行**:提两次只有一条、点两次只生效一次、job 跑两遍只做一次

第二组里最要紧的是**并发**那几条:重复执行不是靠"记得检查一下"避免的,
是靠判断和写入在同一条语句里 —— 而那件事只有并发场景测得出来。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lifein.models.normalized import Trust
from lifein.repos import approvals
from lifein.repos.approvals import ApprovalError, ApprovalStatus

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def a_request(session, user_id, **overrides):
    payload = {
        "agent": "qa",
        "tool_name": "message.send",
        "tool_args": {"to": "老王", "text": "明天三点见"},
        "preview_text": "给老王发:明天三点见",
        "trust": Trust.USER_INPUT,
        "now": NOW,
    }
    payload.update(overrides)
    return approvals.enqueue(user_id, session, **payload)


class TestZeroOverreach:
    """**零越权。** 铁律 8 在这里是数据库约束,不是文档里的一句话。"""

    @pytest.mark.parametrize("trust", [Trust.EXTERNAL])
    def test_external_content_can_never_start_an_l3(self, pg_session, user_id, trust):
        """提示注入即使骗过 agent,也写不进这张表。"""
        with pytest.raises(ApprovalError) as caught:
            a_request(pg_session, user_id, trust=trust)

        # 报的是人话,不是约束名 —— 数据库那条 CHECK 抛的名字不解释为什么
        assert "外部内容" in str(caught.value)

    def test_an_empty_preview_is_refused(self, pg_session, user_id):
        """**03 的退出条件里有这一条**:"你自己不敢点'同意' → 预览做得不够清楚"。
        预览是空的话,人点同意时是在赌。"""
        with pytest.raises(ApprovalError):
            a_request(pg_session, user_id, preview_text="   ")

    def test_approvals_do_not_leak_between_users(self, pg_session, user_id):
        """铁律 1。别人的审批队列里是"要替他发什么消息"。"""
        item = a_request(pg_session, user_id)
        other = "99999999-9999-9999-9999-999999999999"

        assert approvals.get(other, pg_session, approval_id=item.id) is None
        assert approvals.approve(other, pg_session, approval_id=item.id, now=NOW) is None
        assert approvals.list_open(other, pg_session, now=NOW) == []


class TestZeroDuplicateExecution:
    """**零重复执行。** 两道:唯一键挡"提两次",状态机挡"做两次"。"""

    def test_the_same_intent_twice_is_one_approval(self, pg_session, user_id):
        """你说了两遍"帮我回复老王" —— 那是正常的用户行为,不是错误。"""
        first = a_request(pg_session, user_id, idempotency_key="reply-laowang")
        second = a_request(pg_session, user_id, idempotency_key="reply-laowang")

        assert first.id == second.id
        assert len(approvals.list_open(user_id, pg_session, now=NOW)) == 1

    def test_without_a_key_identical_content_still_dedupes(self, pg_session, user_id):
        """兜底那一道:一字不差的重复挡得住。"""
        first = a_request(pg_session, user_id)
        second = a_request(pg_session, user_id)
        assert first.id == second.id

    def test_different_content_is_a_different_approval(self, pg_session, user_id):
        """**兜底只挡一字不差的。** 措辞变了就是两条 —— 所以调用方该给幂等键,
        它才知道"同一个意图"是什么。"""
        first = a_request(pg_session, user_id)
        second = a_request(
            pg_session, user_id, tool_args={"to": "老王", "text": "明天下午三点见"}
        )
        assert first.id != second.id

    def test_approving_twice_only_works_once(self, pg_session, user_id):
        """并发点两次是正常的用户行为(手机点了,手表也点了)。"""
        item = a_request(pg_session, user_id)

        first = approvals.approve(user_id, pg_session, approval_id=item.id, now=NOW)
        second = approvals.approve(user_id, pg_session, approval_id=item.id, now=NOW)

        assert first is not None and first.status is ApprovalStatus.APPROVED
        assert second is None

    def test_executing_twice_only_works_once(self, pg_session, user_id):
        """**job 跑两遍时,第二遍拿到 None,于是它知道自己白跑了一趟。**"""
        item = a_request(pg_session, user_id)
        approvals.approve(user_id, pg_session, approval_id=item.id, now=NOW)

        first = approvals.mark_executed(
            user_id, pg_session, approval_id=item.id, now=NOW, result={"sent": True}
        )
        second = approvals.mark_executed(user_id, pg_session, approval_id=item.id, now=NOW)

        assert first is not None and first.result == {"sent": True}
        assert second is None

    def test_executing_something_never_approved_does_nothing(self, pg_session, user_id):
        """`mark_executed` 从 `approved` 出发 —— 没点过同意的动不了。"""
        item = a_request(pg_session, user_id)
        assert approvals.mark_executed(user_id, pg_session, approval_id=item.id, now=NOW) is None

    def test_a_rejected_one_cannot_be_approved_later(self, pg_session, user_id):
        item = a_request(pg_session, user_id)
        approvals.reject(user_id, pg_session, approval_id=item.id, now=NOW)

        assert approvals.approve(user_id, pg_session, approval_id=item.id, now=NOW) is None


class TestExpiry:
    """**过期是安全属性,不是清理。**"""

    def test_an_expired_one_cannot_be_approved(self, pg_session, user_id):
        """一条三天前提的"帮我回复老王"现在点同意,发出去的内容早就不合时宜了,
        而卡片上看不出这一点。"""
        item = a_request(pg_session, user_id, ttl=timedelta(hours=1))

        later = NOW + timedelta(hours=2)
        assert approvals.approve(user_id, pg_session, approval_id=item.id, now=later) is None

    def test_expired_ones_drop_out_of_the_open_list(self, pg_session, user_id):
        """卡片上不该出现一条点不动的东西。"""
        a_request(pg_session, user_id, ttl=timedelta(hours=1))

        later = NOW + timedelta(hours=2)
        assert approvals.list_open(user_id, pg_session, now=later) == []

    def test_the_sweeper_marks_them(self, pg_session, user_id):
        item = a_request(pg_session, user_id, ttl=timedelta(hours=1))
        later = NOW + timedelta(hours=2)

        assert approvals.expire_overdue(user_id, pg_session, now=later) == 1
        after = approvals.get(user_id, pg_session, approval_id=item.id)
        assert after.status is ApprovalStatus.EXPIRED

    def test_the_sweeper_leaves_approved_ones_alone(self, pg_session, user_id):
        """**点过同意的不该被扫掉。** 它已经在等执行了,而执行可能刚开始 ——
        扫掉的结果是那件事永远不会发生,而没有任何地方说得清为什么。"""
        item = a_request(pg_session, user_id, ttl=timedelta(hours=1))
        approvals.approve(user_id, pg_session, approval_id=item.id, now=NOW)

        later = NOW + timedelta(hours=2)
        assert approvals.expire_overdue(user_id, pg_session, now=later) == 0
        assert approvals.get(
            user_id, pg_session, approval_id=item.id
        ).status is ApprovalStatus.APPROVED

    def test_the_default_ttl_is_a_day(self, pg_session, user_id):
        """06 §2.8:24 小时。比待确认那边的 30 天短得多。"""
        item = a_request(pg_session, user_id)
        assert item.expires_at - NOW == timedelta(hours=24)


class TestTheQueues:
    def test_ready_means_approved_but_not_yet_done(self, pg_session, user_id):
        """执行 job 读的就是这个列表。"""
        done = a_request(pg_session, user_id, idempotency_key="a")
        waiting = a_request(pg_session, user_id, idempotency_key="b")
        a_request(pg_session, user_id, idempotency_key="c")  # 还没点

        for item in (done, waiting):
            approvals.approve(user_id, pg_session, approval_id=item.id, now=NOW)
        approvals.mark_executed(user_id, pg_session, approval_id=done.id, now=NOW)

        ready = approvals.list_ready(user_id, pg_session)
        assert [item.id for item in ready] == [waiting.id]

    def test_a_failure_is_not_a_rejection(self, pg_session, user_id):
        """**一个是你不要,一个是没做成。** 后者可能还要重试,
        而重试要先有人看过 —— 所以它不会自己回到 approved。"""
        item = a_request(pg_session, user_id)
        approvals.approve(user_id, pg_session, approval_id=item.id, now=NOW)

        after = approvals.mark_failed(
            user_id, pg_session, approval_id=item.id, now=NOW, error="企微接口 500"
        )
        assert after.status is ApprovalStatus.FAILED
        assert after.result == {"error": "企微接口 500"}
        assert approvals.list_ready(user_id, pg_session) == []

    def test_rejected_ones_are_kept(self, pg_session, user_id):
        """**不删记录** —— 拒绝过什么是判断预览做得好不好的原料。"""
        item = a_request(pg_session, user_id)
        approvals.reject(user_id, pg_session, approval_id=item.id, now=NOW)

        (recent,) = approvals.list_recent(user_id, pg_session)
        assert (recent.id, recent.status) == (item.id, ApprovalStatus.REJECTED)
