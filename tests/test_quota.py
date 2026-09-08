"""每用户成本上限(P4 第 5 片)。需要真实 PostgreSQL:花了多少是一条聚合。

03 的 P4 范围里那句括号是要害:"主动扫描的开销随用户数线性增长"。
摘要、记忆抽取、日程提取、记账判定每个用户每天都要跑一遍,而它们全都调模型 ——
一个人一天几毛钱,十个人就是十份,**其中九份的账单是你付**。

这一组盯两件事:

1. **算得对**:花了多少直接从 `tool_calls` 求和,不另记一张表
2. **停对了东西**:停的是模型调用,不是采集
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from lifein.repos import quota
from lifein.repos.quota import QuotaExceeded

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def a_call(session, user_id, *, cost: str | None = "0.05", when: datetime = NOW):
    session.execute(
        text(
            "INSERT INTO tool_calls (user_id, agent, tool_name, level, args_digest,"
            " result_status, cost_cny, created_at)"
            " VALUES (:u, 'daily_digest', 'llm.chat', 'L1', '{}'::jsonb, 'allowed',"
            " CAST(:c AS NUMERIC), :t)"
        ),
        {"u": user_id, "c": cost, "t": when},
    )


class TestCounting:
    def test_it_sums_from_tool_calls(self, pg_session, user_id):
        """**不需要第二张计数表。** 多一张表意味着多一处可能和事实对不上的
        地方,而对不上的那次一定是"记的比实际少" —— 漏记一次的代价是
        超了不知道。"""
        for _ in range(3):
            a_call(pg_session, user_id, cost="0.05")

        current = quota.usage(user_id, pg_session, now=NOW)
        assert current.spent == Decimal("0.15")
        assert current.calls == 3

    def test_last_month_does_not_count(self, pg_session, user_id):
        """**按自然月切,和预算那边同一个口径。** 口径不一样的话,"这个月"
        在两个地方指不同的时间段,而那时没有人能对上账。"""
        a_call(pg_session, user_id, when=NOW - timedelta(days=40))

        assert quota.usage(user_id, pg_session, now=NOW).spent == Decimal("0")

    def test_calls_without_a_cost_are_not_counted_as_calls(self, pg_session, user_id):
        """没算出钱的调用(比如没配单价)不该被数成"用了一次额度"。"""
        a_call(pg_session, user_id, cost=None)

        current = quota.usage(user_id, pg_session, now=NOW)
        assert (current.spent, current.calls) == (Decimal("0"), 0)

    def test_it_does_not_reach_across_users(self, pg_session, user_id):
        """铁律 1。**别人的花费不该停掉你的功能** —— 而这个数字正是停不停的依据。"""
        a_call(pg_session, user_id, cost="9.99")
        other = "99999999-9999-9999-9999-999999999999"

        assert quota.usage(other, pg_session, now=NOW).spent == Decimal("0")


class TestTheCap:
    def test_no_cap_never_stops_anything(self, pg_session, user_id):
        """**默认无限,不是一个猜出来的数字** —— 猜出来的上限会在某个月
        安静地停掉一个人的全部功能。"""
        a_call(pg_session, user_id, cost="999.00")

        assert quota.usage(user_id, pg_session, now=NOW, cap=None).over is False
        quota.guard(user_id, pg_session, now=NOW, cap=None)  # 不抛

    def test_under_the_cap_passes(self, pg_session, user_id):
        a_call(pg_session, user_id, cost="1.00")
        quota.guard(user_id, pg_session, now=NOW, cap=Decimal("10"))

    def test_over_the_cap_raises(self, pg_session, user_id):
        a_call(pg_session, user_id, cost="10.00")

        with pytest.raises(QuotaExceeded) as caught:
            quota.guard(user_id, pg_session, now=NOW, cap=Decimal("10"))

        # **报错要说清"采集没停"** —— 否则收到告警的人第一反应是数据丢了
        assert "采集没停" in str(caught.value)
        assert "重跑" in str(caught.value)

    def test_near_the_cap_is_not_over(self, pg_session, user_id):
        """快到了和到了是两件事:前者还在跑,后者停了。"""
        a_call(pg_session, user_id, cost="8.50")

        current = quota.usage(user_id, pg_session, now=NOW, cap=Decimal("10"))
        assert (current.near, current.over) == (True, False)
        quota.guard(user_id, pg_session, now=NOW, cap=Decimal("10"))  # 还是放行

    def test_remaining_is_reported(self, pg_session, user_id):
        a_call(pg_session, user_id, cost="3.00")

        assert quota.usage(
            user_id, pg_session, now=NOW, cap=Decimal("10")
        ).remaining == Decimal("7.00")


@pytest.mark.integration
def test_the_scheduler_skips_instead_of_failing(pg_session, user_id):
    """**超上限是"这一轮跳过",不是"系统坏了"。**

    原文都还在,加回额度之后重跑一遍就补上了 —— 而把它当成失败会让
    job_runs 里留下一串 failed,那时你会去查一个根本不存在的 bug。
    """
    from lifein.config import Settings
    from lifein.scheduler import within_quota
    from tests.test_config import BASE

    a_call(pg_session, user_id, cost="10.00")

    class Loud:
        def __init__(self):
            self.sent = []

        def alert(self, title, body):
            self.sent.append((title, body))

    class FakeServices:
        settings = Settings(_env_file=None, **{**BASE, "monthly_cost_cap_cny": 5.0})

        def __init__(self):
            self.alerter = Loud()

    services = FakeServices()
    assert within_quota(services, pg_session, user_id, now=NOW, job="daily_digest") is False
    assert services.alerter.sent  # 告警给运维,不推给用户
