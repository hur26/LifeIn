"""邮件归一化。

06 §1.3 那一行的实现:

    email | message | Subject | Date 头 | From/To/Cc → email | text/plain 优先,html 降级

架构 §2.1 说归一化"没有技术含量但决定成败"。这个模块就是那句话的第一份证据 ——
它几乎全是在处理别人发过来的畸形数据:没有 Date 头的邮件、编码声明错误的正文、
只有 HTML 没有纯文本的营销邮件、主题长得离谱的自动通知。

**每一种畸形都有明确归宿**,没有"这种情况暂时不管"(06 §1.4):

    缺 Date / 缺 Message-ID   必填字段缺失 → normalize_error,告警,raw 留着
    只有 HTML                 部分降级     → parse_degraded
    正文超长                  截断         → truncated
    字符集解不开              部分降级     → parse_degraded

邮件永远是 `external`(06 §1.2)。哪怕是你自己发给自己的 —— 因为发件人字段
可以伪造,而这个字段的用途是决定"能不能触发 L3",不能靠它可能是真的。
"""

from __future__ import annotations

import email.policy
import hashlib
import html
import re
from datetime import datetime
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from lifein.models.normalized import (
    Attachment,
    EventKind,
    ExternalRef,
    Flag,
    IdentifierType,
    NormalizedEvent,
    Party,
    PartyRole,
    Trust,
)
from lifein.sources.base import IngestedEvent

SOURCE = "email"

BODY_MAX = 20_000
"""正文上限。超出截断并打 truncated。

选 2 万字不是算出来的:一封正常邮件远到不了,而超过这个数的基本是把整份
PDF 转成文本塞进正文的自动邮件,留全文只会撑爆后面的 prompt。
"""

# HTML 降级抽取。刻意不引解析库 —— 引一个要先写 ADR(AGENTS.md §3),
# 而 P0 只需要"能读出大意"。抽得不干净会打上 parse_degraded,
# 如果摘要质量因此变差,那时再带着证据去写那条 ADR。
_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_BREAKS = re.compile(r"<(br|/p|/div|/tr|/li)\s*/?>", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"\n{3,}")

_ROLE_BY_HEADER = {
    "from": PartyRole.FROM,
    "to": PartyRole.TO,
    "cc": PartyRole.CC,
}


def html_to_text(raw_html: str) -> str:
    """把 HTML 压成能读的纯文本。够用即可,不追求还原排版。"""
    text = _SCRIPT_STYLE.sub(" ", raw_html)
    text = _BREAKS.sub("\n", text)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES.sub("\n\n", text).strip()


def _decode_part(part: EmailMessage) -> tuple[str, bool]:
    """返回(文本, 是否降级)。字符集解不开时用替换字符兜底,不抛异常。"""
    try:
        return part.get_content(), False
    except (LookupError, UnicodeDecodeError, KeyError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace"), True


def _extract_body(msg: EmailMessage) -> tuple[str, list[Flag]]:
    flags: list[Flag] = []
    plain: str | None = None
    rich: str | None = None

    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():  # 附件不进正文
            continue
        subtype = part.get_content_subtype()
        text, degraded = _decode_part(part)
        if degraded:
            flags.append(Flag.PARSE_DEGRADED)
        if subtype == "plain" and plain is None:
            plain = text
        elif subtype == "html" and rich is None:
            rich = text

    if plain is not None and plain.strip():
        body = plain
    elif rich is not None:
        body = html_to_text(rich)
        flags.append(Flag.PARSE_DEGRADED)  # 从 HTML 抽的,不是原生纯文本
    else:
        body = ""

    if len(body) > BODY_MAX:
        body = body[:BODY_MAX]
        flags.append(Flag.TRUNCATED)

    return body.strip(), flags


def _extract_parties(msg: EmailMessage) -> list[Party]:
    parties: list[Party] = []
    for header, role in _ROLE_BY_HEADER.items():
        values = msg.get_all(header, [])
        for display_name, address in getaddresses([str(v) for v in values]):
            if not address:
                continue
            parties.append(
                Party(
                    role=role,
                    display_name=display_name or address,
                    identifier=address.lower(),
                    identifier_type=IdentifierType.EMAIL,
                )
            )
    return parties


def _extract_attachments(msg: EmailMessage) -> list[Attachment]:
    """只存文件名与元信息。附件内容不进库 —— 那多半是最敏感的那部分(账单 PDF)。"""
    items: list[Attachment] = []
    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        items.append(
            Attachment(
                filename=filename,
                content_type=part.get_content_type(),
                size_bytes=len(payload) if payload else None,
            )
        )
    return items


def _header(msg: EmailMessage, name: str) -> str | None:
    try:
        value = msg[name]
    except Exception:  # noqa: BLE001 —— 畸形头不该让整封邮件失败
        return None
    return str(value) if value is not None else None


def normalize_email(raw: bytes, *, received_at: datetime) -> IngestedEvent:
    """把一封原始邮件(RFC822 字节)变成一条摄入事件。

    `received_at` 只在拿不到 Date 头时用作 occurred_at 的兜底,
    并且那种情况一定会同时写 normalize_error —— 兜底值不许冒充真实时间。
    """
    fallback_id = hashlib.sha256(raw).hexdigest()
    raw_meta: dict[str, Any] = {"size_bytes": len(raw)}

    try:
        msg: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[assignment]
    except Exception as exc:  # noqa: BLE001
        return IngestedEvent(
            source=SOURCE,
            external_id=fallback_id,
            occurred_at=received_at,
            trust=Trust.EXTERNAL,
            raw={**raw_meta, "parse_exception": str(exc)},
            normalize_error=f"邮件解析失败:{exc}",
        )

    message_id = _header(msg, "Message-ID")
    external_id = message_id.strip() if message_id else fallback_id
    subject = (_header(msg, "Subject") or "").strip()
    date_header = _header(msg, "Date")

    raw_meta.update(
        {
            "message_id": message_id,
            "subject": subject,
            "date": date_header,
            "from": _header(msg, "From"),
            "to": _header(msg, "To"),
            "cc": _header(msg, "Cc"),
        }
    )

    occurred_at: datetime | None = None
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
            occurred_at = parsed if parsed.tzinfo else None
        except (TypeError, ValueError):
            occurred_at = None

    if occurred_at is None:
        # 必填字段缺失那一档:入库、告警、raw 留着,修好之后能重跑
        return IngestedEvent(
            source=SOURCE,
            external_id=external_id,
            occurred_at=received_at,
            trust=Trust.EXTERNAL,
            raw=raw_meta,
            normalize_error=f"Date 头缺失或不可解析:{date_header!r}",
        )

    body, flags = _extract_body(msg)
    if not message_id:
        # 能用,但要留痕:去重键退化成内容哈希,同一封邮件被两次投递会算成两条
        flags.append(Flag.PARTIAL)

    normalized = NormalizedEvent(
        kind=EventKind.MESSAGE,
        title=subject or "(无主题)",
        occurred_at=occurred_at,
        external_ref=ExternalRef(source=SOURCE, external_id=external_id),
        trust=Trust.EXTERNAL,
        confidence=1.0 if message_id and subject else 0.8,
        parties=_extract_parties(msg),
        body=body or None,
        attachments=_extract_attachments(msg),
        flags=flags,
    )

    return IngestedEvent(
        source=SOURCE,
        external_id=external_id,
        occurred_at=occurred_at,
        trust=Trust.EXTERNAL,
        raw=raw_meta,
        normalized=normalized,
    )
