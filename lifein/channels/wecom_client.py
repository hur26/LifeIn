"""企业微信 API 客户端。

ADR-001 选企微做推送与审批入口,ADR-011 又让日历复用同一套自建应用凭据,
所以推送、审批回调、日程读取三条路都从这里走 —— 它是接入层唯一对外的出口。

这个类真正在解决的只有一件事:**access_token 的生命周期**。企微的 token
两小时过期,而"过期"这件事只会以 errcode 42001 的形式在下一次调用里出现。
把它交给每个调用点各自处理,就会出现"平时好好的,凌晨推送偶尔丢一条"这种
最难查的故障。所以 token 在这里缓存、提前刷新、失效时自动重试一次。

**token 不落库。** ADR-009 要求凭据加密存储,但那说的是长期凭据(secret、
授权码);两小时的 token 存起来只是多一份静态的密文和一处要维护的失效逻辑。
它活在内存里,进程重启就重新换一个,代价是一次 HTTP 请求。

**secret 和 token 永远不进日志。** 报错信息里只带 errcode 与 errmsg。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://qyapi.weixin.qq.com/cgi-bin"

TOKEN_REFRESH_MARGIN_S = 300
"""提前 5 分钟换新 token。

不卡着过期时间换,是因为服务端与本机的时钟不必然一致,而差几秒的代价是
一次静默失败的推送。多换几次 token 不花钱。
"""

# token 失效。企微用两个码表达同一件事,都要触发重取
_TOKEN_EXPIRED_CODES = {40014, 42001}

# 凭据本身错了。重试没有意义,必须告警让人去改配置
_AUTH_ERROR_CODES = {40001, 40013, 41002, 60020}

# 频率限制。可重试,但要退避
_RATE_LIMIT_CODES = {45009, 45033, 45011}


class WecomError(RuntimeError):
    """企微返回了非零 errcode。"""

    def __init__(self, errcode: int, errmsg: str, path: str) -> None:
        super().__init__(f"企微 {path} 返回 errcode={errcode}: {errmsg}")
        self.errcode = errcode
        self.errmsg = errmsg


class WecomAuthError(WecomError):
    """corpid / secret / agentid 配错了。**必须告警** —— 重试再多次也是这个结果。"""


class WecomRateLimited(WecomError):
    """撞到频率限制。可重试,调用方应退避。"""


class WecomUnavailable(RuntimeError):
    """网络层失败。可重试。"""


class WecomClient:
    def __init__(
        self,
        *,
        corp_id: str,
        secret: str,
        agent_id: str,
        base_url: str = BASE_URL,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._corp_id = corp_id
        self._secret = secret
        self.agent_id = agent_id
        self._base_url = base_url.rstrip("/")
        self._http = client or httpx.Client(timeout=15.0)
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # ---------- 对外 ----------

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._call("GET", path, params=params)

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("POST", path, json=payload)

    # ---------- token ----------

    def _access_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh and self._token and self._clock() < self._token_expires_at:
            return self._token

        data = self._request(
            "GET",
            "/gettoken",
            params={"corpid": self._corp_id, "corpsecret": self._secret},
        )
        self._raise_for_errcode(data, "/gettoken")

        self._token = data["access_token"]
        expires_in = int(data.get("expires_in", 7200))
        self._token_expires_at = self._clock() + max(expires_in - TOKEN_REFRESH_MARGIN_S, 0)
        log.info("企微 token 已刷新,%d 秒后到期", expires_in)
        return self._token

    def invalidate_token(self) -> None:
        self._token = None
        self._token_expires_at = 0.0

    # ---------- 内部 ----------

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        for attempt in (1, 2):
            query = dict(params or {})
            query["access_token"] = self._access_token(force_refresh=attempt == 2)
            data = self._request(method, path, params=query, json=json)

            errcode = int(data.get("errcode", 0))
            if errcode in _TOKEN_EXPIRED_CODES and attempt == 1:
                # 唯一一种自动重试:token 在两次调用之间过期了。
                # 其他错误重试没有意义,重试反而会重复副作用。
                log.info("企微 token 失效(errcode=%d),换一个重试", errcode)
                self.invalidate_token()
                continue

            self._raise_for_errcode(data, path)
            return data

        raise AssertionError("unreachable")  # pragma: no cover

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            response = self._http.request(method, url, params=params, json=json)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            # 不把 exc 直接往上抛:它的字符串里会带完整 URL,而 URL 上挂着 access_token
            raise WecomUnavailable(f"企微 {path} 请求失败:{type(exc).__name__}") from None

    @staticmethod
    def _raise_for_errcode(data: Mapping[str, Any], path: str) -> None:
        errcode = int(data.get("errcode", 0))
        if errcode == 0:
            return
        errmsg = str(data.get("errmsg", ""))
        if errcode in _AUTH_ERROR_CODES:
            raise WecomAuthError(errcode, errmsg, path)
        if errcode in _RATE_LIMIT_CODES:
            raise WecomRateLimited(errcode, errmsg, path)
        raise WecomError(errcode, errmsg, path)
