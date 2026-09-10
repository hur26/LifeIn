"""channel_state 的集成测试。需要真实 PostgreSQL(见 conftest.py)。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from lifein.repos.channel_state import claim_lease, clear_state, get_state, set_state

pytestmark = pytest.mark.integration

CHANNEL = "weixin"


def test_missing_key_is_none(pg_session, user_id):
    assert get_state(user_id, pg_session, channel=CHANNEL, key="get_updates_buf") is None


def test_set_then_get(pg_session, user_id):
    set_state(user_id, pg_session, channel=CHANNEL, key="buf", value="abc")
    assert get_state(user_id, pg_session, channel=CHANNEL, key="buf") == "abc"


def test_set_overwrites(pg_session, user_id):
    # 游标每轮都要更新,不能每次插一行
    set_state(user_id, pg_session, channel=CHANNEL, key="buf", value="v1")
    set_state(user_id, pg_session, channel=CHANNEL, key="buf", value="v2")
    assert get_state(user_id, pg_session, channel=CHANNEL, key="buf") == "v2"


def test_channels_do_not_collide(pg_session, user_id):
    set_state(user_id, pg_session, channel="weixin", key="buf", value="w")
    set_state(user_id, pg_session, channel="wecom", key="buf", value="c")
    assert get_state(user_id, pg_session, channel="weixin", key="buf") == "w"


def test_another_user_sees_nothing(pg_session, user_id):
    set_state(user_id, pg_session, channel=CHANNEL, key="buf", value="mine")
    other = "99999999-9999-9999-9999-999999999999"
    assert get_state(other, pg_session, channel=CHANNEL, key="buf") is None


def test_clear(pg_session, user_id):
    # 会话重建后游标必须清掉 —— 旧游标对新会话没有意义
    set_state(user_id, pg_session, channel=CHANNEL, key="buf", value="old")
    clear_state(user_id, pg_session, channel=CHANNEL, key="buf")
    assert get_state(user_id, pg_session, channel=CHANNEL, key="buf") is None


class TestTheInboxLease:
    """**一个 iLink token 同时只能有一个长轮询。**

    两个客户端一起拉会互相抢消息 —— 表现是"消息一会儿到一会儿不到",
    而两边的日志各自看起来都正常。这在一台机器上同时跑着开发进程和正式服务时
    会真发生,**而那时你会以为是 iLink 在丢消息**。

    在这条租约之前,那个危险只写在 `weixin_inbound.py` 的注释里 ——
    写着但没挡着。
    """

    def claim(self, session, user_id, owner, *, ttl=timedelta(minutes=3)):
        return claim_lease(
            user_id, session, channel="weixin", key="inbox_lease", owner=owner, ttl=ttl
        )

    def age_the_lease(self, session, user_id, *, by=timedelta(hours=1)):
        """把租约的时间戳往回拨,模拟持有者已经死了。

        **不是把测试的钟往前拨** —— 那条比较在 SQL 里用数据库的 `now()` 做,
        而那正是这个设计要的:写入和比较同一个钟,时钟偏移就不是变量了
        (见 `_CLAIM_LEASE` 的说明)。
        """
        session.execute(
            text(
                "UPDATE channel_state SET updated_at = now() - CAST(:ago AS INTERVAL)"
                " WHERE user_id = :u AND key = 'inbox_lease'"
            ),
            {"u": user_id, "ago": f"{by.total_seconds()} seconds"},
        )

    def test_the_first_one_gets_it(self, pg_session, user_id):
        assert self.claim(pg_session, user_id, "a") is True

    def test_the_second_one_does_not(self, pg_session, user_id):
        self.claim(pg_session, user_id, "a")
        assert self.claim(pg_session, user_id, "b") is False

    def test_the_holder_can_renew(self, pg_session, user_id):
        """续约就是再抢一次。**每一轮都要续** —— 不续的话租约会过期,
        而那时另一个进程会合法地接手,于是两个一起拉。"""
        self.claim(pg_session, user_id, "a")
        assert self.claim(pg_session, user_id, "a") is True
        assert self.claim(pg_session, user_id, "a") is True

    def test_a_dead_holder_is_taken_over(self, pg_session, user_id):
        """进程被 kill 之后,下一个实例最多等一个租约周期就能接手。

        那段时间里消息不会丢 —— 长轮询的游标记着位置。
        """
        self.claim(pg_session, user_id, "a")
        self.age_the_lease(pg_session, user_id)

        assert self.claim(pg_session, user_id, "b") is True
        # 接手之后原来那个再来续就续不上了 —— 它该退出
        assert self.claim(pg_session, user_id, "a") is False

    def test_another_users_lease_is_separate(self, pg_session, user_id):
        """铁律 1。两个人各自的微信会话互不相干,租约也是。"""
        self.claim(pg_session, user_id, "a")
        other = "99999999-9999-9999-9999-999999999999"
        assert self.claim(pg_session, other, "a") is True
