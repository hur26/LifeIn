"""邮件归一化的测试。

用例全是真实会遇到的畸形:没有 Date 头、只有 HTML、主题超长、编码声明错误、
中文主题的 RFC2047 编码。摘要质量差的时候,问题多半在这里而不是 prompt
(03 P0 的退出条件就这么写的)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from lifein.models.normalized import Flag, PartyRole, Trust
from lifein.sources.email_source import BODY_MAX, html_to_text, normalize_email

RECEIVED = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


def build_mail(
    *,
    subject: str = "季度报销单已通过",
    date: str | None = "Mon, 07 Sep 2026 08:30:00 +0800",
    message_id: str | None = "<abc123@qq.com>",
    plain: str | None = "报销单已通过,金额 1280 元。",
    html_body: str | None = None,
    attachment: tuple[str, bytes] | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "财务部 <finance@example.com>"
    msg["To"] = "me@qq.com"
    if date:
        msg["Date"] = date
    if message_id:
        msg["Message-ID"] = message_id
    if plain is not None:
        msg.set_content(plain)
    if html_body is not None:
        if plain is None:
            msg.set_content(html_body, subtype="html")
        else:
            msg.add_alternative(html_body, subtype="html")
    if attachment:
        name, data = attachment
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg.as_bytes()


def test_normal_mail():
    e = normalize_email(build_mail(), received_at=RECEIVED)
    assert e.failed is False
    n = e.normalized
    assert n.title == "季度报销单已通过"
    assert n.trust is Trust.EXTERNAL
    assert n.occurred_at.hour == 8  # +0800,不是收件时间
    assert "1280" in n.body


def test_sender_is_external_even_when_it_is_yourself():
    """发件人字段可以伪造,而 trust 决定的是能不能触发 L3。"""
    msg = EmailMessage()
    msg["Subject"] = "给自己的备忘"
    msg["From"] = "me@qq.com"
    msg["To"] = "me@qq.com"
    msg["Date"] = "Mon, 07 Sep 2026 08:30:00 +0800"
    msg["Message-ID"] = "<self@qq.com>"
    msg.set_content("记得交房租")

    e = normalize_email(msg.as_bytes(), received_at=RECEIVED)
    assert e.normalized.trust is Trust.EXTERNAL


def test_parties_carry_role_and_typed_identifier():
    n = normalize_email(build_mail(), received_at=RECEIVED).normalized
    roles = {p.role: p for p in n.parties}
    assert roles[PartyRole.FROM].identifier == "finance@example.com"
    assert roles[PartyRole.FROM].display_name == "财务部"
    assert roles[PartyRole.TO].identifier == "me@qq.com"


def test_missing_date_becomes_normalize_error_not_a_guess():
    # 必填字段缺失那一档:入库、告警、raw 留着,不许拿收件时间冒充发生时间
    e = normalize_email(build_mail(date=None), received_at=RECEIVED)
    assert e.failed is True
    assert "Date" in e.normalize_error
    assert e.raw["subject"] == "季度报销单已通过"  # raw 保留,修好能重跑


def test_unparseable_date_is_also_an_error():
    e = normalize_email(build_mail(date="昨天下午"), received_at=RECEIVED)
    assert e.failed is True


def test_html_only_mail_is_degraded_not_dropped():
    e = normalize_email(
        build_mail(plain=None, html_body="<p>账单<b>已出</b></p><script>x()</script>"),
        received_at=RECEIVED,
    )
    n = e.normalized
    assert "账单" in n.body
    assert "script" not in n.body.lower()
    assert Flag.PARSE_DEGRADED in n.flags


def test_plain_wins_over_html():
    e = normalize_email(
        build_mail(plain="纯文本正文", html_body="<p>HTML 正文</p>"), received_at=RECEIVED
    )
    assert "纯文本正文" in e.normalized.body
    assert Flag.PARSE_DEGRADED not in e.normalized.flags


def test_oversized_body_is_truncated_and_flagged():
    e = normalize_email(build_mail(plain="字" * (BODY_MAX + 500)), received_at=RECEIVED)
    assert len(e.normalized.body) == BODY_MAX
    assert Flag.TRUNCATED in e.normalized.flags


def test_missing_message_id_falls_back_to_content_hash_and_flags_partial():
    e = normalize_email(build_mail(message_id=None), received_at=RECEIVED)
    assert e.failed is False
    assert len(e.external_id) == 64  # sha256
    assert Flag.PARTIAL in e.normalized.flags


def test_attachment_metadata_only():
    # 附件内容不进库:那多半是最敏感的那部分
    e = normalize_email(
        build_mail(attachment=("对账单.pdf", b"%PDF-1.4 fake")), received_at=RECEIVED
    )
    att = e.normalized.attachments[0]
    assert att.filename == "对账单.pdf"
    assert att.content_type == "application/pdf"
    assert att.size_bytes == len(b"%PDF-1.4 fake")


def test_no_subject_still_produces_a_title():
    e = normalize_email(build_mail(subject=""), received_at=RECEIVED)
    assert e.normalized.title == "(无主题)"
    assert e.normalized.confidence < 1.0  # 信息不全,置信度要降


def test_garbage_bytes_do_not_raise():
    e = normalize_email(b"\x00\x01 not an email at all", received_at=RECEIVED)
    # 解析不出来也要入库,不能让一封坏邮件卡住整轮采集
    assert e.failed is True
    assert e.occurred_at == RECEIVED


@pytest.mark.parametrize(
    ("raw_html", "expected"),
    [
        ("<p>一</p><p>二</p>", "一\n二"),
        ("<style>.a{}</style>正文", "正文"),
        ("&amp;&lt;", "&<"),
    ],
)
def test_html_to_text(raw_html, expected):
    assert html_to_text(raw_html) == expected


class TestStubPlainPart:
    """真跑第一天漏掉一场面试的原因。

    有些邮件塞一句"新面试"当预览纯文本段,真正内容全在 HTML 里。无条件优先
    text/plain 的话,抽出来的正文是三个字 —— 而系统连 flag 都不打,
    看起来一切正常,只是那封邮件对摘要毫无贡献。
    """

    def build(self, plain: str, html_len: int = 600) -> bytes:
        return build_mail(plain=plain, html_body=f"<p>{'详情' * (html_len // 2)}</p>")

    def test_stub_plain_falls_back_to_html(self):
        e = normalize_email(self.build("新面试"), received_at=RECEIVED)
        assert len(e.normalized.body) > 200
        assert Flag.PARSE_DEGRADED in e.normalized.flags  # 换过来了要留痕

    def test_substantial_plain_still_wins(self):
        """判据刻意保守 —— 正常的纯文本不该被带页脚导航的 HTML 版本顶掉。"""
        real = "这是一封正常的纯文本邮件正文" * 6
        e = normalize_email(self.build(real), received_at=RECEIVED)
        assert e.normalized.body.startswith("这是一封正常")
        assert Flag.PARSE_DEGRADED not in e.normalized.flags

    def test_short_plain_with_short_html_is_left_alone(self):
        # HTML 也没多少内容时,没有理由推翻纯文本
        e = normalize_email(build_mail(plain="收到", html_body="<p>收到</p>"), received_at=RECEIVED)
        assert e.normalized.body == "收到"
