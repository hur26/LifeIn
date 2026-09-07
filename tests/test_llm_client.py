"""LLM 客户端的测试。

重点在重试边界:该重的重、不该重的不重。重错了会把一次失败放大成三次,
而这是要花钱的。
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from lifein.llm.client import (
    LLMBadResponse,
    LLMClient,
    LLMError,
    LLMResponse,
    LLMUnavailable,
)

OK_BODY = {
    "model": "some-model",
    "choices": [{"message": {"content": "今天有 2 件要紧事。"}}],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
}


class Server:
    """按脚本回响应,并记下请求次数。"""

    def __init__(self, script: list) -> None:
        self.script = script
        self.calls = 0
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.requests.append(request)
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def build(script: list, **kw) -> tuple[LLMClient, Server, list[float]]:
    server = Server(script)
    slept: list[float] = []
    client = LLMClient(
        base_url="https://llm.example.com/v1",
        api_key="sk-secret",
        model="some-model",
        client=httpx.Client(transport=httpx.MockTransport(server)),
        sleep=slept.append,
        **kw,
    )
    return client, server, slept


def msgs() -> list[dict[str, str]]:
    return [{"role": "user", "content": "你好"}]


def test_successful_call():
    client, server, _ = build([httpx.Response(200, json=OK_BODY)])
    out = client.chat(msgs())

    assert out.text == "今天有 2 件要紧事。"
    assert out.prompt_tokens == 1000
    assert server.calls == 1


def test_api_key_goes_in_the_header_not_the_url():
    client, server, _ = build([httpx.Response(200, json=OK_BODY)])
    client.chat(msgs())

    request = server.requests[0]
    assert request.headers["Authorization"] == "Bearer sk-secret"
    assert "sk-secret" not in str(request.url)


def test_no_response_format_is_sent():
    """结构化输出各家支持程度不一,发了反而是兼容性风险。"""
    import json as _json

    client, server, _ = build([httpx.Response(200, json=OK_BODY)])
    client.chat(msgs())
    assert "response_format" not in _json.loads(server.requests[0].content)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_statuses_are_retried(status):
    client, server, slept = build(
        [httpx.Response(status, json={}), httpx.Response(200, json=OK_BODY)]
    )
    assert client.chat(msgs()).text
    assert server.calls == 2
    assert slept == [1.0]


def test_timeout_is_retried():
    client, server, _ = build(
        [httpx.TimeoutException("timed out"), httpx.Response(200, json=OK_BODY)]
    )
    assert client.chat(msgs()).text
    assert server.calls == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retried(status):
    # 参数错、密钥错这类重试只会把一次失败变成三次,而这是要花钱的
    client, server, _ = build([httpx.Response(status, json={"error": "bad"})])
    with pytest.raises(LLMError):
        client.chat(msgs())
    assert server.calls == 1


def test_gives_up_after_max_retries():
    client, server, _ = build([httpx.Response(503, json={})], max_retries=2)
    with pytest.raises(LLMUnavailable):
        client.chat(msgs())
    assert server.calls == 3  # 首次 + 2 次重试


def test_retry_after_header_wins_over_backoff():
    # 服务端说了等多久就等多久,它比我们清楚
    client, _, slept = build(
        [
            httpx.Response(429, json={}, headers={"Retry-After": "7"}),
            httpx.Response(200, json=OK_BODY),
        ]
    )
    client.chat(msgs())
    assert slept == [7.0]


def test_malformed_response_shape_is_reported():
    client, _, _ = build([httpx.Response(200, json={"choices": []})])
    with pytest.raises(LLMBadResponse):
        client.chat(msgs())


def test_error_message_does_not_leak_the_key():
    client, _, _ = build([httpx.Response(401, json={"error": "bad key"})])
    with pytest.raises(LLMError) as exc:
        client.chat(msgs())
    assert "sk-secret" not in str(exc.value)


class TestJsonParsing:
    def test_plain_json(self):
        assert LLMResponse(text='{"a": 1}', model="m").as_json() == {"a": 1}

    def test_fenced_json_is_accepted(self):
        # 模型不听话地加围栏是最常见的形态,为此让一整天的摘要失败不值得
        fenced = '```json\n{"a": 1}\n```'
        assert LLMResponse(text=fenced, model="m").as_json() == {"a": 1}

    def test_fence_without_language(self):
        assert LLMResponse(text='```\n{"a": 1}\n```', model="m").as_json() == {"a": 1}

    def test_garbage_raises(self):
        with pytest.raises(LLMBadResponse):
            LLMResponse(text="我觉得应该是这样的", model="m").as_json()


class TestCost:
    def test_no_price_means_no_cost(self):
        client, _, _ = build([httpx.Response(200, json=OK_BODY)])
        assert client.chat(msgs()).cost_cny is None

    def test_cost_is_computed_from_both_sides(self):
        client, _, _ = build(
            [httpx.Response(200, json=OK_BODY)],
            price_prompt_per_1k=Decimal("0.002"),
            price_completion_per_1k=Decimal("0.008"),
        )
        # 1000 输入 * 0.002 + 500 输出 * 0.008 = 0.002 + 0.004
        assert client.chat(msgs()).cost_cny == Decimal("0.0060")
