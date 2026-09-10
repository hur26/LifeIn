"""微信入站循环的测试。

三种失败要分开对待,这是这个模块唯一的复杂度 —— 每种一条用例。
最要紧的是会话过期那条:轮询一个死掉的会话看起来和"一切正常"没有区别。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from lifein.alerts import CollectingAlerter
from lifein.channels.base import InboundMessage
from lifein.channels.weixin import WeixinSessionExpired, WeixinUnavailable
from lifein.channels.weixin_inbound import PollResult
from lifein.jobs import weixin_inbox

pytestmark = pytest.mark.integration

USER_MSG = InboundMessage(
    channel="weixin",
    sender="peer-1",
    msg_type="text",
    content="报销批了吗",
    msg_id="m-1",
    created_at=None,  # type: ignore[arg-type]
    channel_ref="ctx-1",
)


class ScriptedPoller:
    """按脚本返回;脚本用完就置停止位,免得测试跑成死循环。"""

    def __init__(self, script: list, stop: threading.Event) -> None:
        self.script = list(script)
        self.stop = stop
        self.calls = 0

    def poll_once(self, *, base_url, token, sync_buf):
        self.calls += 1
        if not self.script:
            self.stop.set()
            return PollResult(messages=[], sync_buf=sync_buf, context_tokens={})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@dataclass
class FakeServices:
    settings: object
    alerter: CollectingAlerter
    llm: object = None
    channel: object = None
    resolve_user: object = None


@pytest.fixture
def configured_user(pg_session, user_id):
    from lifein.config import Settings
    from lifein.repos import credentials, users
    from tests.test_config import BASE

    settings = Settings(_env_file=None, **BASE)
    credentials.put_credential(
        user_id,
        pg_session,
        kind="weixin",
        scope="query",
        payload={"token": "tok", "to_user_id": "peer-1", "base_url": "https://x"},
        settings=settings,
    )
    del users
    return user_id, settings


def factory(session):
    from contextlib import contextmanager

    @contextmanager
    def _open():
        yield session

    return _open


def run(pg_session, user_id, settings, script, handled=None):
    stop = threading.Event()
    alerter = CollectingAlerter()
    services = FakeServices(settings=settings, alerter=alerter)
    poller = ScriptedPoller(script, stop)

    if handled is not None:
        weixin_inbox._handle_one = (  # noqa: SLF001
            lambda uid, msg, **kw: handled.append(msg)
        )

    weixin_inbox.run_inbox(
        user_id,
        services=services,
        session_factory=factory(pg_session),
        stop=stop,
        poller=poller,
        sleep=lambda _s: None,
    )
    return alerter, poller


def test_unconfigured_user_exits_immediately(pg_session, user_id):
    from lifein.config import Settings
    from tests.test_config import BASE

    alerter, poller = run(pg_session, user_id, Settings(_env_file=None, **BASE), [])
    assert poller.calls == 0  # 连一轮都没轮询
    assert alerter.alerts == []


def test_messages_are_dispatched(pg_session, configured_user, monkeypatch):
    user_id, settings = configured_user
    handled: list = []
    monkeypatch.setattr(weixin_inbox, "_handle_one", lambda uid, msg, **kw: handled.append(msg))

    run(
        pg_session,
        user_id,
        settings,
        [PollResult(messages=[USER_MSG], sync_buf="buf-1", context_tokens={"peer-1": "ctx-1"})],
    )
    assert [m.content for m in handled] == ["报销批了吗"]


def test_cursor_is_persisted_every_round(pg_session, configured_user, monkeypatch):
    """只在退出时存等于没存 —— 进程被 kill 的时候什么都来不及做。"""
    user_id, settings = configured_user
    monkeypatch.setattr(weixin_inbox, "_handle_one", lambda uid, msg, **kw: None)

    run(pg_session, user_id, settings, [PollResult([], "buf-7", {})])

    from lifein.repos.channel_state import get_state

    assert get_state(user_id, pg_session, channel="weixin", key="get_updates_buf") == "buf-7"


def test_context_token_is_persisted(pg_session, configured_user, monkeypatch):
    user_id, settings = configured_user
    monkeypatch.setattr(weixin_inbox, "_handle_one", lambda uid, msg, **kw: None)

    run(pg_session, user_id, settings, [PollResult([], "b", {"peer-1": "ctx-9"})])

    from lifein.repos.channel_state import get_state

    assert get_state(user_id, pg_session, channel="weixin", key="context_token:peer-1") == "ctx-9"


def test_session_expired_stops_and_alerts(pg_session, configured_user):
    """轮询一个死掉的会话看起来和"一切正常"没有区别 —— 必须停下来喊。"""
    user_id, settings = configured_user
    alerter, poller = run(pg_session, user_id, settings, [WeixinSessionExpired("过期")])

    assert poller.calls == 1  # 立刻停,不重试
    assert alerter.alerts[0][0] == "微信会话已过期"
    assert "login-weixin" in alerter.alerts[0][1]  # 告诉人怎么修


def test_transient_failure_is_retried(pg_session, configured_user, monkeypatch):
    user_id, settings = configured_user
    monkeypatch.setattr(weixin_inbox, "_handle_one", lambda uid, msg, **kw: None)

    alerter, poller = run(
        pg_session,
        user_id,
        settings,
        [WeixinUnavailable("抖动"), PollResult([], "buf-2", {})],
    )
    # 两条脚本都被消费了 = 失败之后接着轮询,没有退出
    assert poller.script == []
    assert alerter.alerts == []  # 抖一下不值得告警


def test_repeated_failures_give_up_and_alert(pg_session, configured_user):
    user_id, settings = configured_user
    alerter, poller = run(
        pg_session,
        user_id,
        settings,
        [WeixinUnavailable("挂了")] * weixin_inbox.MAX_CONSECUTIVE_FAILURES,
    )
    assert poller.calls == weixin_inbox.MAX_CONSECUTIVE_FAILURES
    assert alerter.alerts[0][0] == "微信入站连续失败"


def test_a_second_poller_exits_instead_of_stealing_messages(pg_session, configured_user):
    """**一个 iLink token 同时只能有一个长轮询。**

    两个客户端一起拉会互相抢消息 —— 表现是"消息一会儿到一会儿不到",
    而两边的日志各自看起来都正常。在这条租约之前,那个危险只写在
    `weixin_inbound.py` 的注释里:写着,但没挡着。

    这里让别人先占住租约,再启动这一个 —— 它该立刻退出,一次都不拉。
    """
    from datetime import timedelta

    from lifein.jobs.weixin_inbox import LEASE_KEY
    from lifein.repos import channel_state

    user_id, settings = configured_user
    channel_state.claim_lease(
        user_id,
        pg_session,
        channel="weixin",
        key=LEASE_KEY,
        owner="另一个进程",
        ttl=timedelta(minutes=3),
    )

    alerter, poller = run(pg_session, user_id, settings, [])

    assert poller.calls == 0, "租约在别人手上时一次都不该拉"
    # **不告警。** 这不是故障 —— 多半是刚重启,旧进程还没退干净,
    # 而一条会自己好的告警只会让人慢慢学会忽略告警
    assert alerter.alerts == []


def test_the_holder_keeps_polling_across_rounds(pg_session, configured_user):
    """**每一轮都要续租。** 不续的话租约会过期,而那时另一个进程会
    合法地接手 —— 于是两个一起拉。"""
    user_id, settings = configured_user

    rounds = [PollResult(messages=[], sync_buf=f"buf-{i}", context_tokens={}) for i in range(3)]
    _alerter, poller = run(pg_session, user_id, settings, rounds)

    # 三轮脚本 + 最后一轮空转(它负责置停止位)
    assert poller.calls == 4
