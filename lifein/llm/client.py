"""外部模型客户端 —— OpenAI 兼容接口,不绑厂商。

只用两个端点:`/chat/completions` 与(P1)`/embeddings`。不装厂商 SDK,
理由在 [ADR-017](../../docs/04-tech-decisions.md):一层 SDK 换来的是它对特定
厂商响应格式的假设,而"换厂商只改三行环境变量"这句话得真的成立。

**不发 `response_format`。** 结构化输出各家支持程度不一,有的直接报错、
有的静默忽略。所以要 JSON 就在 prompt 里说,回来自己容错解析(带 ```json
围栏是最常见的形态)。少一个兼容性变量,比省几行解析代码值。

**重试只针对"再试一次可能就好了"的错误**:超时、连接失败、429、5xx。
业务错误(400 参数不对、401 密钥不对)重试没有意义,只会把一次失败变成三次。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class LLMError(RuntimeError):
    """调模型失败。"""


class LLMUnavailable(LLMError):
    """重试若干次后仍然失败。可以稍后再来 —— 每日摘要允许晚几分钟。"""


class LLMBadResponse(LLMError):
    """模型回了东西,但不是我们要的形状。"""


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_cny: Decimal | None = None

    def as_json(self) -> Any:
        """容错解析:剥掉 ```json 围栏再解。

        模型不听话地加围栏是最常见的形态,为此让一整天的摘要失败不值得。
        """
        raw = self.text.strip()
        fenced = _JSON_FENCE.match(raw)
        if fenced:
            raw = fenced.group(1)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMBadResponse(f"模型返回的不是合法 JSON:{exc}") from exc


class LLMClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: int = 60,
        max_retries: int = 2,
        price_prompt_per_1k: Decimal = Decimal(0),
        price_completion_per_1k: Decimal = Decimal(0),
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._max_retries = max_retries
        self._price_prompt = price_prompt_per_1k
        self._price_completion = price_completion_per_1k
        self._sleep = sleep
        self._http = client or httpx.Client(timeout=timeout_s)

    def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        data = self._post_with_retry("/chat/completions", payload)

        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMBadResponse(f"响应里没有 choices[0].message.content:{data}") from exc

        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        return LLMResponse(
            text=text,
            model=str(data.get("model", self._model)),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_cny=self._cost(prompt_tokens, completion_tokens),
        )

    # ---------- 内部 ----------

    def _cost(self, prompt_tokens: int | None, completion_tokens: int | None) -> Decimal | None:
        if not self._price_prompt and not self._price_completion:
            return None  # 没填单价就是不记成本(07 §2.3)
        prompt = Decimal(prompt_tokens or 0) / 1000 * self._price_prompt
        completion = Decimal(completion_tokens or 0) / 1000 * self._price_completion
        return (prompt + completion).quantize(Decimal("0.0001"))

    def _post_with_retry(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        last: str = ""

        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}"
                if attempt < self._max_retries:
                    self._backoff(attempt, last)
                    continue
                raise LLMUnavailable(f"调模型失败({last}),已重试 {attempt} 次") from None

            if response.status_code in RETRYABLE_STATUS:
                last = f"HTTP {response.status_code}"
                if attempt < self._max_retries:
                    self._backoff(attempt, last, response)
                    continue
                raise LLMUnavailable(f"调模型失败({last}),已重试 {attempt} 次")

            if response.status_code >= 400:
                # 参数错、密钥错这类,重试只会把一次失败变成三次
                raise LLMError(f"调模型失败:HTTP {response.status_code}")

            try:
                return response.json()
            except ValueError as exc:
                raise LLMBadResponse("模型返回的不是 JSON") from exc

        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff(self, attempt: int, reason: str, response: httpx.Response | None = None) -> None:
        delay = float(2**attempt)
        if response is not None:
            # 服务端说了等多久就等多久 —— 它比我们清楚
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = float(retry_after)
        log.info("调模型失败(%s),%.0f 秒后重试", reason, delay)
        self._sleep(delay)
