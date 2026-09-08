"""iLink 长轮询的测试。

三条最要紧的都在这里:超时不算故障、重复投递不重复回答、群消息不走这条路。
错一条的表现都是"偶尔漏消息"或者"重复回答",而那种问题在生产上极难复现。
"""

from __future__ import annotations

import json

import httpx
import pytest

from lifein.channels.weixin import WeixinSessionExpired, WeixinUnavailable
from lifein.channels.weixin_inbound import WeixinPoller, context_token_key

BASE = "https://ilink.example.com"
TOKEN = "tok-secret"


def text_msg(**overrides) -> dict:
    base = {
        "from_user_id": "peer-1",
        "msg_id": "m-1",
        "create_time": 1788764400,
        "context_token": "ctx-9",
        "item_list": [{"type": 1, "text_item": {"text": "报销批了吗"}}],
    }
    return {**base, **overrides}


def build(response) -> tuple[WeixinPoller, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(response, Exception):
            raise response
        return httpx.Response(200, json=response)

    return WeixinPoller(client=httpx.Client(transport=httpx.MockTransport(handler))), seen


def poll(poller: WeixinPoller, sync_buf: str = "buf-0"):
    return poller.poll_once(base_url=BASE, token=TOKEN, sync_buf=sync_buf)


def test_text_message_is_parsed():
    poller, seen = build({"ret": 0, "msgs": [text_msg()], "get_updates_buf": "buf-1"})
    result = poll(poller)

    assert len(result.messages) == 1
    msg = result.messages[0]
    assert msg.channel == "weixin"
    assert msg.sender == "peer-1"
    assert msg.content == "报销批了吗"
    assert msg.channel_ref == "ctx-9"
    assert result.sync_buf == "buf-1"


def test_context_token_is_reported_for_persistence():
    # 协议要求回复时原样带回对方最近一次的 context_token
    poller, _ = build({"ret": 0, "msgs": [text_msg()]})
    assert poll(poller).context_tokens == {"peer-1": "ctx-9"}
    assert context_token_key("peer-1") == "context_token:peer-1"


def test_cursor_is_sent_back():
    poller, seen = build({"ret": 0, "msgs": []})
    poll(poller, sync_buf="buf-7")
    assert json.loads(seen[0].content)["get_updates_buf"] == "buf-7"


def test_cursor_survives_an_empty_response():
    # 服务端没给新游标时不能把它清空,否则下一轮会从头拉
    poller, _ = build({"ret": 0, "msgs": []})
    assert poll(poller, sync_buf="buf-7").sync_buf == "buf-7"


def test_timeout_is_normal_not_a_failure():
    """服务端挂 35 秒没消息就返回。当成故障去退避,消息延迟会越退越大。"""
    poller, _ = build(httpx.TimeoutException("long poll"))
    result = poll(poller, sync_buf="buf-3")

    assert result.messages == []
    assert result.sync_buf == "buf-3"  # 游标原样带回,下一轮继续


def test_duplicate_delivery_is_dropped():
    # 重投会让同一个问题被回答两次 —— 用户看到两条,而且花两次模型钱
    poller, _ = build({"ret": 0, "msgs": [text_msg(), text_msg()]})
    assert len(poll(poller).messages) == 1


def test_duplicate_across_polls_is_dropped():
    poller, _ = build({"ret": 0, "msgs": [text_msg()]})
    assert len(poll(poller).messages) == 1
    assert len(poll(poller).messages) == 0


def test_group_messages_are_ignored():
    """群摘要走安卓通知监听(ADR-010),不从这条路来。"""
    poller, _ = build({"ret": 0, "msgs": [text_msg(room_id="room-1")]})
    assert poll(poller).messages == []


def test_message_without_sender_is_skipped():
    poller, _ = build({"ret": 0, "msgs": [text_msg(from_user_id="")]})
    assert poll(poller).messages == []


def test_non_text_item_becomes_unsupported():
    # 图片、语音先不处理,但要能被识别出来回一句人话
    poller, _ = build({"ret": 0, "msgs": [text_msg(item_list=[{"type": 2}])]})
    msg = poll(poller).messages[0]
    assert msg.msg_type == "unsupported"
    assert msg.content == ""


def test_bad_create_time_falls_back_to_now():
    poller, _ = build({"ret": 0, "msgs": [text_msg(create_time="昨天")]})
    assert poll(poller).messages[0].created_at is not None


def test_session_expired_stops_the_loop():
    # 继续轮询一个已经死掉的会话没有意义,调用方要停下来告警
    poller, _ = build({"ret": -14})
    with pytest.raises(WeixinSessionExpired):
        poll(poller)


def test_network_error_does_not_leak_the_token():
    poller, _ = build(httpx.ConnectError("boom"))
    with pytest.raises(WeixinUnavailable) as exc:
        poll(poller)
    assert TOKEN not in str(exc.value)
