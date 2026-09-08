"""iLink 入站:长轮询收消息。

**一个微信账号上只能有一个客户端做这件事。** 长轮询共用同一个 bot 会话,
两个客户端同时拉会互相抢消息 —— 所以给 LifeIn 单独扫一个 bot
([ADR-018](../../docs/04-tech-decisions.md) 的重评触发条件里写了这一条)。

三件事必须做对,少一件都会表现成"偶尔漏消息"或者"重复回答":

1. **游标要持久化。** `get_updates_buf` 是服务端给的续传位置,存进
   `channel_state`。不存的话进程重启会重放旧消息 —— 重复回答、重复花钱。
2. **同一条消息可能被投递两次。** 长轮询的重连、服务端重投都会造成重复,
   所以按 `msg_id` 在时间窗内去重。
3. **`context_token` 要记住。** 协议要求回复时原样带回对方最近一次的值。
   记在 `channel_state` 里,按对方 id 分开。

**长轮询本身超时不是错误。** 服务端挂 35 秒没消息就返回,这是正常节奏,
不该当成故障去退避 —— 那会让消息延迟越退越大。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from lifein.channels.base import InboundMessage
from lifein.channels.weixin import (
    APP_ID,
    CHANNEL_VERSION,
    ITEM_TYPE_TEXT,
    WeixinSessionExpired,
    WeixinUnavailable,
    build_headers,
    raise_for_errcode,
)

log = logging.getLogger(__name__)

UPDATES_ENDPOINT = "ilink/bot/getupdates"
LONG_POLL_TIMEOUT_S = 40.0
"""比服务端的 35 秒挂起稍长 —— 短了会在服务端正常返回之前就把连接断掉,
表现是消息一直收不到,而两边日志都看不出错。"""

DEDUP_TTL_S = 300

CHANNEL = "weixin"
SYNC_BUF_KEY = "get_updates_buf"
CONTEXT_TOKEN_PREFIX = "context_token:"


@dataclass
class _Dedup:
    """按 msg_id 在时间窗内去重。进程内即可 —— 重启后游标也重置,不会重放太多。"""

    ttl_s: float = DEDUP_TTL_S
    _seen: dict[str, float] = field(default_factory=dict)

    def is_new(self, msg_id: str, *, now: float) -> bool:
        self._evict(now)
        if msg_id in self._seen:
            return False
        self._seen[msg_id] = now
        return True

    def _evict(self, now: float) -> None:
        expired = [k for k, at in self._seen.items() if now - at > self.ttl_s]
        for key in expired:
            del self._seen[key]


@dataclass
class PollResult:
    messages: list[InboundMessage]
    sync_buf: str
    context_tokens: dict[str, str]
    """本轮收到的 `发送者 → context_token`,调用方负责存起来。"""


class WeixinPoller:
    """一次长轮询。**不自带循环** —— 循环和持久化交给调用方,

    这样它在测试里是个纯函数式的东西:给一个响应,拿一批消息。
    """

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._http = client or httpx.Client(timeout=LONG_POLL_TIMEOUT_S)
        self._dedup = _Dedup()

    def poll_once(self, *, base_url: str, token: str, sync_buf: str) -> PollResult:
        payload = {
            "get_updates_buf": sync_buf,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        try:
            response = self._http.post(
                f"{base_url.rstrip('/')}/{UPDATES_ENDPOINT}",
                content=body.encode(),
                headers={**build_headers(token, body), "iLink-App-Id": APP_ID},
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            # 挂起到超时是长轮询的正常节奏,不是故障。原样返回游标继续下一轮
            return PollResult(messages=[], sync_buf=sync_buf, context_tokens={})
        except httpx.HTTPError as exc:
            raise WeixinUnavailable(f"iLink 长轮询失败:{type(exc).__name__}") from None
        except ValueError as exc:
            raise WeixinUnavailable("iLink 长轮询返回的不是 JSON") from exc

        # 会话过期在这里抛 WeixinSessionExpired,调用方应该停下来告警,
        # 而不是继续轮询一个已经死掉的会话
        raise_for_errcode(data)

        new_buf = str(data.get("get_updates_buf") or sync_buf)
        messages: list[InboundMessage] = []
        tokens: dict[str, str] = {}
        moment = time.monotonic()

        for raw in data.get("msgs") or []:
            parsed = self._parse(raw, now=moment)
            if parsed is None:
                continue
            messages.append(parsed)
            if parsed.channel_ref:
                tokens[parsed.sender] = parsed.channel_ref

        return PollResult(messages=messages, sync_buf=new_buf, context_tokens=tokens)

    def _parse(self, raw: dict, *, now: float) -> InboundMessage | None:
        sender = str(raw.get("from_user_id") or "").strip()
        if not sender:
            return None

        room_id = str(raw.get("room_id") or raw.get("chat_room_id") or "").strip()
        if room_id:
            # 群事件对多数 bot 类型根本不投递,真收到了也不处理:
            # 群摘要走的是安卓通知监听(ADR-010),不从这里来
            log.debug("忽略群消息,群摘要不走这条路")
            return None

        msg_id = str(raw.get("msg_id") or raw.get("client_id") or "").strip()
        if msg_id and not self._dedup.is_new(msg_id, now=now):
            log.debug("重复投递,跳过 msg_id=%s", msg_id)
            return None

        text = _extract_text(raw.get("item_list") or [])
        return InboundMessage(
            channel=CHANNEL,
            sender=sender,
            msg_type="text" if text else "unsupported",
            content=text,
            msg_id=msg_id,
            created_at=_to_datetime(raw.get("create_time")),
            channel_ref=str(raw.get("context_token") or "") or None,
        )


def _extract_text(item_list: list) -> str:
    parts: list[str] = []
    for item in item_list:
        if not isinstance(item, dict) or item.get("type") != ITEM_TYPE_TEXT:
            continue
        text = str((item.get("text_item") or {}).get("text") or "")
        if text:
            parts.append(text)
    return "\n".join(parts)


def _to_datetime(value: object) -> datetime:
    try:
        seconds = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return datetime.now(UTC)
    if not (0 < seconds < 4_102_444_800):
        return datetime.now(UTC)
    return datetime.fromtimestamp(seconds, tz=UTC)


def context_token_key(sender: str) -> str:
    return f"{CONTEXT_TOKEN_PREFIX}{sender}"


__all__ = [
    "CHANNEL",
    "SYNC_BUF_KEY",
    "PollResult",
    "WeixinPoller",
    "WeixinSessionExpired",
    "context_token_key",
]


def iter_forever(
    poller: WeixinPoller,
    *,
    base_url: str,
    token: str,
    load_buf: Callable[[], str],
    save_buf: Callable[[str], None],
) -> Iterator[PollResult]:  # pragma: no cover - 循环本身没有可测的逻辑
    """一直轮询下去。**每轮都持久化游标** —— 只在退出时存等于没存。"""
    while True:
        result = poller.poll_once(base_url=base_url, token=token, sync_buf=load_buf())
        save_buf(result.sync_buf)
        yield result
