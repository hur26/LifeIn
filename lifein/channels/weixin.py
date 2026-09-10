"""微信推送通道 —— 走腾讯的 iLink Bot API([ADR-018](../../docs/04-tech-decisions.md))。

**只做出站。** 收消息是长轮询同一个 bot 会话,如果这个微信账号上还跑着别的
iLink 客户端,两边会互相抢消息。出站没有这个问题,所以先把确定的收益拿到手。

**扫码登录不在这里做。** P0 用已有的会话:`token` / `base_url` / `to_user_id`
存进 `credentials` 表(加密),用 `python -m lifein.admin set-weixin` 配。
自己实现二维码登录是一大块,真需要时再补 —— 现在做等于为一个已经解决的问题
写代码。

**会话会过期。** `errcode=-14` 就是这个意思,过期后必须重新扫码。
这时候通道会抛 `WeixinSessionExpired`,**调用方应该降级到企微而不是重试** ——
重试一百次也还是过期的。
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from lifein.channels.base import Card, Delivery

log = logging.getLogger(__name__)

BASE_URL = "https://ilinkai.weixin.qq.com"
SEND_ENDPOINT = "ilink/bot/sendmessage"

# 协议常量。这些值来自 iLink 的客户端约定,不是我们能选的
APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
CLIENT_VERSION = (2 << 16) | (2 << 8) | 0
MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2
ITEM_TYPE_TEXT = 1

SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2

MAX_TEXT_CHARS = 4000
"""iLink 单条消息的上限。**这是协议给的,不是我们挑的。**"""

MAX_CHUNKS = 5
"""超长的最多切成几条。

**切而不是截断**是后补的:原来超过 4000 字直接砍掉后半段,而砍掉的
恰恰是月度报告里按类目列的那些数字 —— 报告的开头是套话,结尾才是内容。

但也不能无限切:一条卡片变成二十条消息,在聊天窗口里就是刷屏,
而**刷屏比截断更让人想关掉推送**。五条之后仍然放不下的,那条截断提示
就是诚实的说法。
"""

CHUNK_GAP_S = 0.3
"""两条之间隔多久。

连着发会撞 iLink 的限频(`errcode=-2`),而限频丢掉的是**后面那几条** ——
表现是"摘要只发了一半",和截断长得一模一样却更难查。
"""

TRUNCATION_NOTE = "\n\n…(内容过长已截断)"


class WeixinError(RuntimeError):
    """iLink 调用失败。"""


class WeixinSessionExpired(WeixinError):
    """会话过期,必须重新扫码。**不要重试** —— 重试一百次也是过期的。"""


class WeixinUnavailable(WeixinError):
    """网络层失败或被限频。可重试,也可以直接降级。"""


@dataclass(frozen=True)
class WeixinSession:
    """一次扫码登录的产物。存 `credentials` 表,加密。"""

    token: str
    to_user_id: str
    """推送目标 —— 和 bot 对话的那个人的 iLink user id。"""

    base_url: str = BASE_URL
    context_token: str | None = None


class WeixinChannel:
    name = "weixin"

    def __init__(
        self,
        *,
        load_session: Callable[[str], WeixinSession | None],
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # 会话按用户取:P0 只有一个人,但通道不该知道这件事
        self._load_session = load_session
        self._http = client or httpx.Client(timeout=15.0)
        # 注入是为了测:分块之间要等 0.3 秒,而没有人愿意为了跑一次用例真等
        self._sleep = sleep

    def send(self, user_id: str, card: Card) -> Delivery:
        session = self._load_session(user_id)
        if session is None:
            raise WeixinError(f"用户 {user_id} 没有可用的微信会话,先跑 admin set-weixin")

        chunks = split(render_text(card))

        # **中途失败就抛。** 前几条已经出去了,而调用方会降级到邮件重发整份 ——
        # 于是用户在微信里看到半份、在邮箱里看到整份。那是有意的:
        # 半份加整份仍然是"收到了",而吞掉异常只会让他收到半份还以为是全部
        delivery: Delivery | None = None
        for index, chunk in enumerate(chunks):
            if index:
                self._sleep(CHUNK_GAP_S)
            delivery = self._send_one(session, chunk)

        assert delivery is not None  # split 至少返回一条
        return Delivery(
            channel=delivery.channel,
            delivery_id=delivery.delivery_id,
            truncated=chunks[-1].endswith(TRUNCATION_NOTE),
        )

    def _send_one(self, session: WeixinSession, text: str) -> Delivery:
        payload = self._build_payload(session, text)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        try:
            response = self._http.post(
                f"{session.base_url.rstrip('/')}/{SEND_ENDPOINT}",
                content=body.encode(),
                headers=build_headers(session.token, body),
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            # 不透传原异常:它的字符串里带完整 URL,而 header 里有 token
            raise WeixinUnavailable(f"iLink 请求失败:{type(exc).__name__}") from None
        except ValueError as exc:
            raise WeixinError("iLink 返回的不是 JSON") from exc

        raise_for_errcode(data)

        return Delivery(
            channel=self.name,
            delivery_id=str(data.get("msgid") or payload["msg"]["client_id"]),
        )

    @staticmethod
    def _build_payload(session: WeixinSession, text: str) -> dict:
        message: dict = {
            "from_user_id": "",
            "to_user_id": session.to_user_id,
            # 幂等键。iLink 靠它识别重复投递,所以每条消息必须是新的
            "client_id": str(uuid.uuid4()),
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": MESSAGE_STATE_FINISH,
            "item_list": [{"type": ITEM_TYPE_TEXT, "text_item": {"text": text}}],
        }
        if session.context_token:
            message["context_token"] = session.context_token
        return {"msg": message, "base_info": {"channel_version": CHANNEL_VERSION}}


def render_text(card: Card) -> str:
    """把通道中立的 Card 渲染成纯文本。**不切,不截断** —— 那是 `split` 的事。

    **不用 markdown。** 微信聊天窗口不渲染它,写 `**加粗**` 就是原样显示两个
    星号 —— 这是它和企微通道最大的区别,两边渲染各写各的正是 `Card` 中立的意义。
    """
    blocks: list[str] = []
    if card.title.strip():
        blocks.append(card.title.strip())
    if card.summary.strip():
        blocks.append(card.summary.strip())

    for section in card.sections:
        lines: list[str] = []
        if section.heading:
            lines.append(f"【{section.heading}】")
        lines.extend(f"· {line}" for line in section.lines)
        if lines:
            blocks.append("\n".join(lines))

    if card.footer:
        blocks.append(f"—— {card.footer}")

    return "\n\n".join(blocks).strip() or "(空)"


def split(text: str, *, limit: int = MAX_TEXT_CHARS, max_chunks: int = MAX_CHUNKS) -> list[str]:
    """把一段文本切成能发出去的几条。**在语义边界上切。**

    三级边界,一级切不开才往下走:段(`\n\n`)→ 行(`\n`)→ 硬切。
    直接按字数硬切的话,一句话会断在半路,而一张按类目列数字的报告
    会被切得两边都读不懂。

    切到 `max_chunks` 还放不下的,最后一条末尾挂截断提示 ——
    **那时它是诚实的**:确实放不下了,而不是"懒得切"。

    多于一条时每条前面加 `(i/n)`:三条消息接连进来,不标的话看起来像
    推了三次,而**"它今天推了三次"是最容易让人关掉推送的印象**。
    """
    if len(text) <= limit:
        return [text]

    # 预留编号的位置。`(10/10)\n` 是最长的那种,按它算
    marker = len("(10/10)\n")
    room = limit - marker

    chunks: list[str] = []
    for piece in _pieces(text, room):
        if chunks and len(chunks[-1]) + 2 + len(piece) <= room:
            chunks[-1] = f"{chunks[-1]}\n\n{piece}"
        else:
            chunks.append(piece)

    if len(chunks) > max_chunks:
        chunks = chunks[:max_chunks]
        tail = chunks[-1][: room - len(TRUNCATION_NOTE)]
        chunks[-1] = tail + TRUNCATION_NOTE

    if len(chunks) == 1:
        return chunks
    return [f"({i}/{len(chunks)})\n{chunk}" for i, chunk in enumerate(chunks, start=1)]


def _pieces(text: str, room: int) -> list[str]:
    """按段拆,放不下的按行拆,还放不下的硬切。"""
    out: list[str] = []
    for block in text.split("\n\n"):
        if len(block) <= room:
            out.append(block)
            continue
        for line in block.split("\n"):
            if len(line) <= room:
                out.append(line)
                continue
            # 一行就超了(比如一段没有换行的长正文)。**只有这里才硬切**
            out.extend(line[i : i + room] for i in range(0, len(line), room))
    return [piece for piece in out if piece]


def build_headers(token: str, body: str) -> dict[str, str]:
    """iLink 要求的一组头。入站长轮询也用它,所以是公开的。"""
    # X-WECHAT-UIN 是每次请求一个随机值,不是身份标识
    uin = base64.b64encode(str(int.from_bytes(secrets.token_bytes(4), "big")).encode()).decode()
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "Content-Length": str(len(body.encode())),
        "X-WECHAT-UIN": uin,
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": str(CLIENT_VERSION),
    }


def raise_for_errcode(data: dict) -> None:
    """把 iLink 的返回码翻译成异常。入站出站共用 —— 错误码表只该有一份。"""
    ret = data.get("ret")
    errcode = data.get("errcode")
    errmsg = str(data.get("errmsg") or "")

    if SESSION_EXPIRED_ERRCODE in (ret, errcode):
        raise WeixinSessionExpired("微信会话已过期,需要重新扫码")

    # -2 有两种含义:限频,以及 errmsg 为 unknown error 时的"会话其实已经废了"。
    # 后者当成限频去退避会一直失败,所以要分开
    if RATE_LIMIT_ERRCODE in (ret, errcode):
        if errmsg.lower() == "unknown error":
            raise WeixinSessionExpired("微信会话已失效(-2/unknown error),需要重新扫码")
        raise WeixinUnavailable("iLink 限频")

    if ret not in (None, 0) or errcode not in (None, 0):
        raise WeixinError(f"iLink 返回 ret={ret} errcode={errcode}: {errmsg}")
