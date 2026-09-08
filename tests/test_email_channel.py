"""邮件兜底通道的测试。不需要数据库,也不连 SMTP。

两件事必须测到:

- **防自我回环**:采集的邮箱和发信的邮箱常常是同一个。不跳过的话,系统昨天
  发出去的摘要今天会被自己采回来当成新事件,进而进摘要和记忆 ——
  而那条"事实"指回的来源是系统自己
- **发不出去时抛异常**:降级链靠异常判断"这条通道不行,换下一条"。
  吞掉异常等于让链条以为发成功了,而用户什么也没收到
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from lifein.channels.base import Card, CardSection
from lifein.channels.email import (
    MARKER_HEADER,
    SUBJECT_PREFIX,
    EmailChannel,
    build_message,
    looks_like_our_own_push,
    render_text,
)
from lifein.channels.fallback import FallbackChannel


class FakeSender:
    def __init__(self, boom: Exception | None = None) -> None:
        self.sent: list[EmailMessage] = []
        self._boom = boom

    def send(self, message: EmailMessage) -> None:
        if self._boom:
            raise self._boom
        self.sent.append(message)


def card() -> Card:
    return Card(
        title="9 月 8 日摘要",
        summary="今天有 2 件要紧事。",
        sections=[CardSection(heading="要做的事", lines=["交房租", "回财务的邮件"])],
        footer="来自 5 条事件",
    )


def channel(sender=None) -> tuple[EmailChannel, FakeSender]:
    sender = sender or FakeSender()
    return (
        EmailChannel(
            sender, from_address="me@example.com", resolve_address=lambda _u: "me@example.com"
        ),
        sender,
    )


def test_sends_a_plain_text_message():
    ch, sender = channel()

    delivery = ch.send("u1", card())

    [message] = sender.sent
    assert message["Subject"] == f"{SUBJECT_PREFIX}9 月 8 日摘要"
    assert message.get_content_type() == "text/plain"
    assert delivery.channel == "email"
    assert delivery.delivery_id == message["Message-ID"]


def test_every_outgoing_message_carries_the_marker():
    """标记是防回环的依据,少一封就漏一封。"""
    ch, sender = channel()
    ch.send("u1", card())
    assert sender.sent[0][MARKER_HEADER] == "1"


def test_body_keeps_the_summary_first():
    # 手机通知栏里看到的就是标题和这一行,和别的通道保持一致
    body = render_text(card())
    assert body.splitlines()[0] == "今天有 2 件要紧事。"
    assert "【要做的事】" in body
    assert "- 交房租" in body
    assert body.endswith("— 来自 5 条事件\n")


def test_our_own_push_is_recognised_by_the_collector():
    message = build_message(card(), from_address="me@example.com", to_address="me@example.com")
    assert looks_like_our_own_push(message.as_bytes()) is True


def test_a_normal_mail_is_not_skipped():
    normal = EmailMessage()
    normal["Subject"] = "报销单已通过"
    normal["From"] = "finance@corp.com"
    normal.set_content("金额 1280 元")
    assert looks_like_our_own_push(normal.as_bytes()) is False


def test_the_marker_is_only_looked_for_in_the_headers():
    """正文里出现这个字符串不算 —— 比如你把告警邮件转发给自己。

    那封信是别人发来的,该被正常采集。
    """
    forwarded = EmailMessage()
    forwarded["Subject"] = "转发:系统告警"
    forwarded["From"] = "colleague@corp.com"
    forwarded.set_content(f"下面是原文\n{MARKER_HEADER}: 1\n告警内容……")
    assert looks_like_our_own_push(forwarded.as_bytes()) is False


def test_send_failure_propagates_so_the_chain_can_react():
    ch, _ = channel(FakeSender(boom=OSError("SMTP 连不上")))
    with pytest.raises(OSError):
        ch.send("u1", card())


def test_email_is_the_last_resort_in_the_fallback_chain():
    """前两条通道的共同失效方式是平台,邮件不依赖任何平台政策(R6)。

    所以它排最后,而且必须真的接得住。
    """

    class Dead:
        name = "weixin"

        def send(self, user_id, card):
            raise RuntimeError("会话过期")

    alerts: list[tuple[str, str]] = []

    class Recorder:
        def alert(self, title: str, detail: str) -> None:
            alerts.append((title, detail))

    ch, sender = channel()
    chain = FallbackChannel([Dead(), ch], alerter=Recorder())

    delivery = chain.send("u1", card())

    assert delivery.channel == "email"
    assert len(sender.sent) == 1
    assert alerts, "降级必须告警:不然主通道死了两周你也不知道"


# ---------- 配置与告警 ----------


def settings(**overrides):
    from lifein.config import Settings
    from tests.test_config import BASE

    return Settings(_env_file=None, **{**BASE, **overrides})


def test_smtp_is_all_or_nothing():
    """配一半比没配更糟:降级链会多出一条必然失败的通道。

    而失败的表现是每天多一条告警 —— 告警变成噪音之后,真出事的那次就被忽略了。
    """
    with pytest.raises(ValueError):
        settings(smtp_host="smtp.qq.com")  # 缺 username / password / to


def test_no_smtp_config_means_no_email_channel():
    assert settings().smtp_enabled is False


def test_sender_falls_back_to_the_login_name():
    s = settings(
        smtp_host="smtp.qq.com",
        smtp_username="me@qq.com",
        smtp_password="app-code",
        smtp_to="me@qq.com",
    )
    assert s.smtp_enabled is True
    assert s.smtp_sender_address == "me@qq.com"


def test_alerter_falls_back_to_logging_when_the_mail_fails(caplog):
    """告警自己失败不许拖垮调用方 —— 那是用一个小问题换一个大问题。"""
    from lifein.alerts import EmailAlerter

    class Dead:
        name = "email"

        def send(self, user_id, card):
            raise OSError("SMTP 连不上")

    EmailAlerter(Dead(), lambda: "u1").alert("采集器掉线", "两小时没有心跳")
    # 没抛出去就是对的;日志里两条都在(告警本身 + 发送失败)
    assert any("采集器掉线" in r.message or "采集器掉线" in r.getMessage() for r in caplog.records)


def test_alerter_stays_quiet_when_there_is_no_user_yet():
    from lifein.alerts import EmailAlerter

    sender_calls: list[str] = []

    class Recorder:
        name = "email"

        def send(self, user_id, card):
            sender_calls.append(user_id)

    EmailAlerter(Recorder(), lambda: None).alert("启动", "还没有用户")
    assert sender_calls == []
