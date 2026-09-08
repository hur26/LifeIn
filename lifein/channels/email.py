"""邮件通道 —— 降级链的最后一环。

前面两条通道(微信 iLink、企微)有一个共同的失效方式:**平台。** 会话过期、
接口改版、账号被限制,任何一条都会让推送整条断掉(R6)。邮件不会 ——
它是这套系统里唯一不依赖任何平台政策的出口,所以它排在最后。

**它也是告警的出口。** 告警不能走可能已经挂掉的那条通道(07 §2.6),
而"挂掉"恰恰是最需要告警的时候。

两个必须做对的地方:

**防自我回环。** 采集的邮箱和发信的邮箱很可能是同一个 —— 那样系统发出去的
摘要,第二天会被自己采回来变成一条"新事件",进而进摘要和记忆。所以每封信
都带 `X-LifeIn-Push` 头,采集侧见到它就跳过。这不是洁癖:一次回环就会让
记忆里出现"你自己说过的话",而那条事实指回的来源是系统自己。

**纯文本,不发 HTML。** 卡片本来就只有标题、一句话和几段列表,HTML 换来的
是排版,代价是各家邮箱客户端的渲染差异,以及"看起来像营销邮件"——
而营销邮件正是这个系统教用户忽略的东西。
"""

from __future__ import annotations

import logging
import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from lifein.channels.base import Card, Delivery

log = logging.getLogger(__name__)

MARKER_HEADER = "X-LifeIn-Push"
"""自己发出去的信带这个头。采集侧靠它跳过,避免自我回环。"""

SUBJECT_PREFIX = "[LifeIn] "
"""收件箱里一眼认得出。也是头被中转服务剥掉时的第二道识别。"""


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    use_ssl: bool = True
    """465 用 SSL,587 用 STARTTLS。国内邮箱几乎都支持 465,所以默认它。"""

    timeout_s: int = 30


class SmtpSender(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class SmtpTransport:
    """真正发信的那一层。**单独拆出来是为了它能被替换掉** ——

    测试里不该真连 SMTP,而"连不上"这条路径恰恰是最需要测的。
    """

    def __init__(self, config: SmtpConfig) -> None:
        self._config = config

    def send(self, message: EmailMessage) -> None:
        c = self._config
        factory = smtplib.SMTP_SSL if c.use_ssl else smtplib.SMTP
        with factory(c.host, c.port, timeout=c.timeout_s) as server:
            if not c.use_ssl:
                server.starttls()
            server.login(c.username, c.password)
            server.send_message(message)


class EmailChannel:
    name = "email"

    def __init__(
        self,
        sender: SmtpSender,
        *,
        from_address: str,
        resolve_address: Callable[[str], str],
    ) -> None:
        self._sender = sender
        self._from = from_address
        self._resolve = resolve_address
        """user_id → 收件地址。通道自己负责寻址,调用方只认识 user_id。"""

    def send(self, user_id: str, card: Card) -> Delivery:
        to_address = self._resolve(user_id)
        message = build_message(card, from_address=self._from, to_address=to_address)
        self._sender.send(message)
        # 邮件没有平台侧的消息 id 可拿,拿 Message-ID 当 delivery_id ——
        # 它至少能在自己的发件箱里对得上
        return Delivery(channel=self.name, delivery_id=message["Message-ID"] or "")


def build_message(card: Card, *, from_address: str, to_address: str) -> EmailMessage:
    """把卡片渲染成一封纯文本邮件。

    标题进 Subject,`summary` 放正文第一段 —— 手机通知栏能看到的就是这两样,
    和别的通道保持一致(base.py 里那句"用户先看到的是这一行")。
    """
    message = EmailMessage()
    message["Subject"] = f"{SUBJECT_PREFIX}{card.title}"
    message["From"] = from_address
    message["To"] = to_address
    message[MARKER_HEADER] = "1"
    message.set_content(render_text(card))
    # 显式生成 Message-ID:有它才谈得上在发件箱里追一封信
    from email.utils import make_msgid

    message["Message-ID"] = make_msgid(domain="lifein.local")
    return message


def render_text(card: Card) -> str:
    lines = [card.summary, ""]
    for section in card.sections:
        if section.heading:
            lines.append(f"【{section.heading}】")
        lines.extend(f"- {line}" for line in section.lines)
        lines.append("")
    if card.footer:
        lines.append(f"— {card.footer}")
    return "\n".join(lines).strip() + "\n"


def looks_like_our_own_push(raw: bytes) -> bool:
    """这封信是不是我们自己发出去的。

    在**头部区域**里找标记,不扫全文:一封正常邮件的正文里完全可能出现
    这个字符串(比如你把告警邮件转发给自己),而那封信不该被跳过。
    """
    head = raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]
    return MARKER_HEADER.lower().encode() in head.lower()
