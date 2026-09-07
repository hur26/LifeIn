"""企微客户端的测试。

被测的核心只有一件事:**token 生命周期**。企微 token 两小时过期,而"过期"
只以 errcode 42001 的形式在下一次调用里出现 —— 处理不好的表现是"平时好好的,
凌晨推送偶尔丢一条",最难查的那一类。
"""

from __future__ import annotations

import httpx
import pytest

from lifein.channels.wecom_client import (
    TOKEN_REFRESH_MARGIN_S,
    WecomAuthError,
    WecomClient,
    WecomError,
    WecomRateLimited,
    WecomUnavailable,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    """记下每次请求,并按预设脚本回响应。"""

    def __init__(self, script: list[dict]) -> None:
        self.script = script
        self.requests: list[httpx.Request] = []
        self.token_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/gettoken"):
            self.token_calls += 1
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": f"tok-{self.token_calls}", "expires_in": 7200},
            )
        return httpx.Response(200, json=self.script.pop(0))


def build(script: list[dict], clock: FakeClock | None = None) -> tuple[WecomClient, Recorder]:
    recorder = Recorder(script)
    transport = httpx.MockTransport(recorder.handler)
    client = WecomClient(
        corp_id="corp",
        secret="s3cret",
        agent_id="1000002",
        base_url="https://qyapi.example.com/cgi-bin",
        client=httpx.Client(transport=transport),
        clock=clock or FakeClock(),
    )
    return client, recorder


def test_successful_call_carries_the_token():
    client, rec = build([{"errcode": 0, "msgid": "m1"}])
    assert client.post("/message/send", {"touser": "u"})["msgid"] == "m1"

    send = rec.requests[-1]
    assert send.url.params["access_token"] == "tok-1"


def test_token_is_reused_across_calls():
    client, rec = build([{"errcode": 0}, {"errcode": 0}])
    client.post("/message/send", {})
    client.post("/message/send", {})
    assert rec.token_calls == 1  # 每次调用都换 token 是纯浪费


def test_token_is_refreshed_before_it_actually_expires():
    """不卡着过期时间换 —— 服务端与本机时钟不必然一致,差几秒就是一条静默丢失的推送。"""
    clock = FakeClock()
    client, rec = build([{"errcode": 0}, {"errcode": 0}], clock)

    client.post("/message/send", {})
    clock.advance(7200 - TOKEN_REFRESH_MARGIN_S + 1)
    client.post("/message/send", {})

    assert rec.token_calls == 2


def test_expired_token_triggers_exactly_one_retry():
    # token 在两次调用之间过期,是唯一一种值得自动重试的错误
    client, rec = build(
        [{"errcode": 42001, "errmsg": "access_token expired"}, {"errcode": 0, "msgid": "m2"}]
    )
    assert client.post("/message/send", {})["msgid"] == "m2"
    assert rec.token_calls == 2


def test_the_other_token_error_code_also_retries():
    client, rec = build([{"errcode": 40014, "errmsg": "invalid access_token"}, {"errcode": 0}])
    client.post("/message/send", {})
    assert rec.token_calls == 2


def test_token_error_twice_in_a_row_gives_up():
    # 不无限重试:换了新 token 还说无效,那是配置问题,重试只会放大问题
    client, _ = build([{"errcode": 42001, "errmsg": "x"}, {"errcode": 42001, "errmsg": "x"}])
    with pytest.raises(WecomError) as exc:
        client.post("/message/send", {})
    assert exc.value.errcode == 42001


def test_bad_secret_is_an_auth_error_not_a_retry():
    client, _ = build([])
    client._http = httpx.Client(  # noqa: SLF001 —— 直接换掉传输层,模拟 gettoken 就失败
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"errcode": 40001, "errmsg": "invalid credential"})
        )
    )
    with pytest.raises(WecomAuthError):
        client.post("/message/send", {})


def test_rate_limit_has_its_own_type():
    # 调用方要能区分"退避重试"和"去改配置"
    client, _ = build([{"errcode": 45009, "errmsg": "api freq out of limit"}])
    with pytest.raises(WecomRateLimited):
        client.post("/message/send", {})


def test_business_error_is_raised_with_the_code():
    client, _ = build([{"errcode": 81013, "errmsg": "user not found"}])
    with pytest.raises(WecomError) as exc:
        client.post("/message/send", {})
    assert exc.value.errcode == 81013


def test_network_failure_does_not_leak_the_token_in_the_message():
    """httpx 的异常字符串里带完整 URL,而 URL 上挂着 access_token。"""

    def boom(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(
                200, json={"errcode": 0, "access_token": "tok-secret", "expires_in": 7200}
            )
        raise httpx.ConnectError("failed", request=request)

    client = WecomClient(
        corp_id="corp",
        secret="s3cret",
        agent_id="1",
        base_url="https://qyapi.example.com/cgi-bin",
        client=httpx.Client(transport=httpx.MockTransport(boom)),
    )
    with pytest.raises(WecomUnavailable) as exc:
        client.post("/message/send", {})
    assert "tok-secret" not in str(exc.value)
    assert "s3cret" not in str(exc.value)
