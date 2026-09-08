"""channel_state 的集成测试。需要真实 PostgreSQL(见 conftest.py)。"""

from __future__ import annotations

import pytest

from lifein.repos.channel_state import clear_state, get_state, set_state

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
