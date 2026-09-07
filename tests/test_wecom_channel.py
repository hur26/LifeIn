"""企微推送通道的测试。

两条最要紧:内容超长要截断而不是丢失(每天一条摘要,丢一条就是那天什么都
没有),以及卡片里不许出现企微特有的东西 —— 那是换通道时的成本所在。
"""

from __future__ import annotations

import httpx
import pytest

from lifein.channels.base import Card, CardSection
from lifein.channels.wecom import (
    MARKDOWN_MAX_BYTES,
    WecomChannel,
    render_markdown,
)
from lifein.channels.wecom_client import WecomClient

USER = "11111111-1111-1111-1111-111111111111"


def build_channel(capture: list[dict]) -> WecomChannel:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(
                200, json={"errcode": 0, "access_token": "tok", "expires_in": 7200}
            )
        import json as _json

        capture.append(_json.loads(request.content))
        return httpx.Response(200, json={"errcode": 0, "msgid": "msg-1"})

    client = WecomClient(
        corp_id="corp",
        secret="s",
        agent_id="1000002",
        base_url="https://qyapi.example.com/cgi-bin",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return WecomChannel(client, resolve_userid=lambda _uid: "BaiYang")


def sample_card() -> Card:
    return Card(
        title="9 月 7 日摘要",
        summary="今天有 2 件要紧事。",
        sections=[
            CardSection(heading="待办", lines=["交房租", "回复财务的报销邮件"]),
            CardSection(heading="日程", lines=["15:00 面试"]),
        ],
        footer="共处理 34 封邮件",
    )


def test_send_reaches_the_resolved_userid():
    capture: list[dict] = []
    delivery = build_channel(capture).send(USER, sample_card())

    payload = capture[0]
    assert payload["touser"] == "BaiYang"  # 调用方只认识 user_id
    assert payload["msgtype"] == "markdown"
    assert payload["agentid"] == "1000002"
    assert delivery.delivery_id == "msg-1"
    assert delivery.truncated is False


def test_rendering_uses_only_the_supported_syntax():
    """企微 markdown 不支持列表,写 `-` 会原样显示成一个减号。"""
    content, _ = render_markdown(sample_card())

    assert content.startswith("# 9 月 7 日摘要")
    assert "· 交房租" in content
    assert "\n- " not in content
    assert "**待办**" in content
    assert "> 共处理 34 封邮件" in content


def test_section_without_heading_still_renders():
    content, _ = render_markdown(Card(title="t", summary="s", sections=[CardSection(lines=["一"])]))
    assert "· 一" in content


def test_empty_summary_does_not_leave_a_blank_line():
    content, _ = render_markdown(Card(title="t", summary=""))
    assert content == "# t"


def test_oversized_content_is_truncated_not_dropped():
    # 超了服务端直接报错整条丢失。每天一条摘要,丢一条就是那天什么都没有
    card = Card(title="摘要", summary="", sections=[CardSection(lines=["很长的一行内容"] * 300)])
    content, truncated = render_markdown(card)

    assert truncated is True
    assert len(content.encode()) <= MARKDOWN_MAX_BYTES
    assert content.endswith("内容过长已截断")


def test_truncation_cuts_on_character_boundary():
    """按字节切会切出半个汉字,那半个字节让整条消息的编码失效。"""
    card = Card(title="标", summary="", sections=[CardSection(lines=["中" * 2000])])
    content, truncated = render_markdown(card)

    assert truncated is True
    content.encode().decode()  # 能原样解回来就说明没切坏


def test_truncation_is_reported_on_the_delivery():
    # 摘要变短可能是截断导致的,不是模型偷懒 —— 要能在 push_log 里看出来
    capture: list[dict] = []
    card = Card(title="摘要", summary="", sections=[CardSection(lines=["很长的一行"] * 400)])
    assert build_channel(capture).send(USER, card).truncated is True


def test_duplicate_check_is_disabled():
    # 每日摘要天天结构相似,让企微去判重会误杀
    capture: list[dict] = []
    build_channel(capture).send(USER, sample_card())
    assert capture[0]["enable_duplicate_check"] == 0


def test_card_carries_no_wecom_specific_fields():
    """卡片是通道中立的:它描述"要说什么",不描述"长什么样"。"""
    fields = set(Card.__dataclass_fields__)
    assert fields == {"title", "summary", "sections", "footer"}


@pytest.mark.parametrize("bad_title", ["", "   "])
def test_channel_still_sends_when_title_is_blank(bad_title):
    # 标题空不该让推送失败 —— 宁可难看也要送到
    capture: list[dict] = []
    build_channel(capture).send(USER, Card(title=bad_title, summary="有内容"))
    assert capture[0]["markdown"]["content"]
