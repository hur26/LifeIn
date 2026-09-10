"""微信 iLink 通道的测试。

重点在**错误分类**:iLink 用同一个错误码表达两件性质完全不同的事,
分错了会让系统在会话过期时不停退避重试,而重试一百次也是过期的。
"""

from __future__ import annotations

import json

import httpx
import pytest

from lifein.channels.base import Card, CardSection
from lifein.channels.weixin import (
    CHUNK_GAP_S,
    MAX_CHUNKS,
    MAX_TEXT_CHARS,
    WeixinChannel,
    WeixinError,
    WeixinSession,
    WeixinSessionExpired,
    WeixinUnavailable,
    render_text,
    split,
)

USER = "11111111-1111-1111-1111-111111111111"


def session(**overrides) -> WeixinSession:
    base = dict(token="tok-secret", to_user_id="peer-1", base_url="https://ilink.example.com")
    return WeixinSession(**{**base, **overrides})


def build(
    response,
    *,
    sess: WeixinSession | None = None,
    sleep=lambda _s: None,
) -> tuple[WeixinChannel, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(response, Exception):
            raise response
        return httpx.Response(200, json=response)

    channel = WeixinChannel(
        load_session=lambda _uid: session() if sess is None else sess,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        # 默认不真等:分块之间那 0.3 秒乘以几条,会让整套测试肉眼可见地变慢
        sleep=sleep,
    )
    return channel, seen


def card() -> Card:
    return Card(
        title="9 月 7 日摘要",
        summary="今天有 2 件要紧事。",
        sections=[CardSection(heading="要做的事", lines=["交房租", "回财务的邮件"])],
        footer="来自 34 条事件",
    )


def test_successful_send():
    channel, seen = build({"ret": 0, "msgid": "m-1"})
    delivery = channel.send(USER, card())

    assert delivery.channel == "weixin"
    assert delivery.delivery_id == "m-1"
    assert delivery.truncated is False

    body = json.loads(seen[0].content)
    assert body["msg"]["to_user_id"] == "peer-1"
    assert body["msg"]["item_list"][0]["text_item"]["text"].startswith("9 月 7 日摘要")


def test_token_goes_in_the_header():
    channel, seen = build({"ret": 0})
    channel.send(USER, card())

    assert seen[0].headers["Authorization"] == "Bearer tok-secret"
    assert "tok-secret" not in str(seen[0].url)


def test_each_message_gets_a_fresh_client_id():
    """client_id 是幂等键,重复用会让第二条被 iLink 当成重投丢掉。"""
    channel, seen = build({"ret": 0})
    channel.send(USER, card())
    channel.send(USER, card())

    ids = [json.loads(r.content)["msg"]["client_id"] for r in seen]
    assert ids[0] != ids[1]


def test_context_token_is_included_when_present():
    channel, seen = build({"ret": 0}, sess=session(context_token="ctx-1"))
    channel.send(USER, card())
    assert json.loads(seen[0].content)["msg"]["context_token"] == "ctx-1"


def test_context_token_is_omitted_when_absent():
    channel, seen = build({"ret": 0})
    channel.send(USER, card())
    assert "context_token" not in json.loads(seen[0].content)["msg"]


def test_missing_session_is_a_clear_error():
    channel = WeixinChannel(load_session=lambda _uid: None)
    with pytest.raises(WeixinError) as exc:
        channel.send(USER, card())
    assert "set-weixin" in str(exc.value)


class TestErrorClassification:
    """iLink 用同一个码表达两件事,分错了系统会一直重试一个永远不会好的错误。"""

    def test_session_expired_is_not_retryable(self):
        channel, _ = build({"ret": -14})
        with pytest.raises(WeixinSessionExpired):
            channel.send(USER, card())

    def test_errcode_field_is_checked_too(self):
        # ret 和 errcode 两个字段都可能带错误码
        channel, _ = build({"errcode": -14, "errmsg": "session expired"})
        with pytest.raises(WeixinSessionExpired):
            channel.send(USER, card())

    def test_minus_two_with_unknown_error_is_a_dead_session(self):
        """-2 + unknown error 不是限频,是会话已经废了 —— 退避重试会一直失败。"""
        channel, _ = build({"ret": -2, "errmsg": "unknown error"})
        with pytest.raises(WeixinSessionExpired):
            channel.send(USER, card())

    def test_minus_two_with_a_real_message_is_rate_limiting(self):
        channel, _ = build({"ret": -2, "errmsg": "frequency limit"})
        with pytest.raises(WeixinUnavailable):
            channel.send(USER, card())

    def test_other_errors_carry_the_code(self):
        channel, _ = build({"ret": -99, "errmsg": "something else"})
        with pytest.raises(WeixinError) as exc:
            channel.send(USER, card())
        assert "-99" in str(exc.value)

    def test_network_failure_does_not_leak_the_token(self):
        channel, _ = build(httpx.ConnectError("boom"))
        with pytest.raises(WeixinUnavailable) as exc:
            channel.send(USER, card())
        assert "tok-secret" not in str(exc.value)


class TestRendering:
    def test_plain_text_not_markdown(self):
        """微信聊天窗口不渲染 markdown,写 ** 就是原样显示两个星号。"""
        text = render_text(card())
        assert "**" not in text
        assert "#" not in text
        assert "【要做的事】" in text
        assert "· 交房租" in text

    def test_footer_is_visually_separated(self):
        assert "—— 来自 34 条事件" in render_text(card())

    def test_empty_card_still_produces_something(self):
        # 宁可发一句"(空)"也不要抛异常 —— 那会让当天完全没有摘要
        assert render_text(Card(title="", summary="")) == "(空)"


class TestSplitting:
    """**超长的切开发,不是砍掉后半段。**

    原来超过 4000 字直接截断,而砍掉的恰恰是月度报告里按类目列的那些数字 ——
    报告的开头是套话,结尾才是内容。
    """

    def big(self, blocks: int = 12, lines: int = 40) -> str:
        """造一份有结构的长文本:几个类目,每个类目底下一堆行。

        **形状要像真的那一份** —— 月度报告就是这个样子,而"切在哪里"
        只有对着有结构的文本才验得出来。
        """
        body = "\n".join(f"· 第{j}行 内容内容内容内容" for j in range(lines))
        return "\n\n".join(f"【类目{i}】\n{body}" for i in range(blocks))

    def test_short_text_is_one_chunk_with_no_marker(self):
        """**一条的时候不加编号。** 加了的话每天那条摘要顶上都挂着
        "(1/1)",而那是一个只会让人困惑的数字。"""
        assert split("就一句话") == ["就一句话"]

    def test_every_chunk_fits(self):
        for chunk in split(self.big()):
            assert len(chunk) <= MAX_TEXT_CHARS

    def test_nothing_is_lost(self):
        """**切开之后内容要还在。** 这一条是这个函数存在的全部理由 ——
        它替换的那个实现丢掉了后半段。"""
        text = self.big()
        rejoined = "".join(
            chunk.split("\n", 1)[1] if chunk.startswith("(") else chunk for chunk in split(text)
        )
        for i in range(12):
            assert f"【类目{i}】" in rejoined
        assert "第39行" in rejoined

    def test_it_splits_on_semantic_boundaries(self):
        """段落不许断在半路。**一张按类目列数字的报告切错地方,
        两边都读不懂。**"""
        chunks = split(self.big())

        assert len(chunks) > 1
        for chunk in chunks:
            body = chunk.split("\n", 1)[1]
            assert body.startswith("【类目") or body.startswith("· ")

    def test_chunks_are_numbered(self):
        """三条消息接连进来,不标的话看起来像推了三次 ——
        而"它今天推了三次"是最容易让人关掉推送的印象。"""
        chunks = split(self.big())
        assert chunks[0].startswith(f"(1/{len(chunks)})")
        assert chunks[-1].startswith(f"({len(chunks)}/{len(chunks)})")

    def test_a_single_giant_line_is_hard_cut(self):
        """一行就超了(一段没有换行的长正文)。**只有这时候才硬切。**"""
        chunks = split("啊" * 9000)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= MAX_TEXT_CHARS

    def test_beyond_the_chunk_limit_it_admits_truncation(self):
        """**不能无限切。** 一条卡片变成二十条消息在聊天窗口里就是刷屏,
        而刷屏比截断更让人想关掉推送。那时那句截断提示是诚实的说法。"""
        chunks = split(self.big(blocks=60))

        assert len(chunks) == MAX_CHUNKS
        assert chunks[-1].endswith("已截断)")


class TestSendingInPieces:
    def test_each_chunk_is_one_request(self):
        channel, calls = build({"ret": 0})
        big = Card(title="摘要", summary="", sections=[CardSection(lines=["很长的一行"] * 900)])

        channel.send(USER, big)

        assert len(calls) > 1, "超长的要切开发,不是发一条截断的"

    def test_it_waits_between_chunks(self):
        """连着发会撞 iLink 的限频,而限频丢掉的是**后面那几条** ——
        表现是"摘要只发了一半",和截断长得一模一样却更难查。"""
        waits: list[float] = []
        channel, calls = build({"ret": 0}, sleep=waits.append)
        big = Card(title="摘要", summary="", sections=[CardSection(lines=["很长的一行"] * 900)])

        channel.send(USER, big)

        assert len(waits) == len(calls) - 1, "只在两条之间等,第一条前面不等"
        assert all(w == CHUNK_GAP_S for w in waits)

    def test_a_short_card_is_still_one_request(self):
        channel, calls = build({"ret": 0})
        channel.send(USER, card())
        assert len(calls) == 1

    def test_truncation_is_reported_on_the_delivery(self):
        """切到上限还放不下时,调用方要知道 —— 那条信息进 `push_log`,
        而"这条摘要是不是完整的"以后只有它答得上来。"""
        huge = Card(
            title="摘要",
            summary="",
            sections=[CardSection(lines=["很长的一行"] * 9000)],
        )
        channel, _ = build({"ret": 0})
        assert channel.send(USER, huge).truncated is True

    def test_a_split_card_that_fits_is_not_reported_as_truncated(self):
        big = Card(title="摘要", summary="", sections=[CardSection(lines=["很长的一行"] * 900)])
        channel, _ = build({"ret": 0})
        assert channel.send(USER, big).truncated is False
