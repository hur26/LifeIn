"""IMAP 收信的测试。

不连真邮箱 —— 连接工厂是注入的,这里用一个假的 IMAP 服务端。
被测的是三件在真实环境里最容易出错、又最难现场调试的事:
163 的 ID 握手、不把邮件标成已读、认证失败要炸得明确。
"""

from __future__ import annotations

import imaplib
from datetime import UTC, datetime

import pytest

from lifein.sources.email_adapter import EmailAdapter
from lifein.sources.imap_client import (
    ImapAuthFailed,
    ImapConfig,
    ImapMailbox,
    ImapUnavailable,
)
from tests.test_email_normalize import build_mail

SINCE = datetime(2026, 9, 1, tzinfo=UTC)


class FakeIMAP:
    def __init__(self, *, mails: dict[str, bytes] | None = None, login_ok: bool = True) -> None:
        self.mails = mails or {}
        self.login_ok = login_ok
        self.commands: list[tuple] = []
        self.fetch_specs: list[str] = []
        self.selected_readonly: bool | None = None

    def login(self, user, password):
        self.commands.append(("login", user))
        if not self.login_ok:
            raise imaplib.IMAP4.error("LOGIN failed: 授权码错误")
        return "OK", [b""]

    def select(self, mailbox, readonly=False):
        self.commands.append(("select", mailbox))
        self.selected_readonly = readonly
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "SEARCH":
            return "OK", [b" ".join(k.encode() for k in self.mails)]
        if command == "FETCH":
            uid, spec = args[0], args[1]
            self.fetch_specs.append(spec)
            return "OK", [(b"1 (BODY[] {n}", self.mails[uid])]
        raise AssertionError(command)

    def _simple_command(self, name, *args):
        self.commands.append(("_simple_command", name, *args))
        return "OK", [b""]

    def _untagged_response(self, typ, dat, name):
        return typ, dat

    def close(self):
        self.commands.append(("close",))

    def logout(self):
        self.commands.append(("logout",))


def mailbox(server: FakeIMAP, host: str = "imap.qq.com") -> ImapMailbox:
    config = ImapConfig(host=host, username="me@qq.com", auth_code="16位授权码")
    return ImapMailbox(config, connect=lambda _cfg: server)


def test_netease_gets_the_id_handshake():
    # 163 / 126 不先发 ID 会被回 Unsafe Login
    server = FakeIMAP(mails={"1": build_mail()})
    list(mailbox(server, "imap.163.com").fetch_raw_since(SINCE))

    sent = [c for c in server.commands if c[0] == "_simple_command"]
    assert sent and sent[0][1] == "ID"
    # ID 必须在 SELECT 之前
    order = [c[0] for c in server.commands]
    assert order.index("_simple_command") < order.index("select")


def test_qq_does_not_get_the_id_handshake():
    server = FakeIMAP(mails={"1": build_mail()})
    list(mailbox(server, "imap.qq.com").fetch_raw_since(SINCE))
    assert not [c for c in server.commands if c[0] == "_simple_command"]


@pytest.mark.parametrize("host", ["imap.163.com", "imap.126.com", "imap.yeah.net"])
def test_all_netease_domains_need_id(host):
    assert ImapConfig(host=host, username="u", auth_code="a").needs_id_handshake


def test_mail_is_not_marked_as_read():
    """系统读了你的邮箱,不该改变你自己看到的样子。"""
    server = FakeIMAP(mails={"1": build_mail()})
    list(mailbox(server).fetch_raw_since(SINCE))

    assert server.fetch_specs == ["(BODY.PEEK[])"]
    assert "RFC822" not in str(server.fetch_specs)
    assert server.selected_readonly is True


def test_auth_failure_raises_a_specific_error():
    # 授权码多半是在改账号密码时失效的,必须炸出来而不是静默停采
    server = FakeIMAP(login_ok=False)
    with pytest.raises(ImapAuthFailed) as exc:
        list(mailbox(server).fetch_raw_since(SINCE))
    assert "授权码" in str(exc.value)


def test_connection_failure_is_retryable_class():
    def boom(_cfg):
        raise OSError("connection refused")

    config = ImapConfig(host="imap.qq.com", username="u", auth_code="a")
    with pytest.raises(ImapUnavailable):
        list(ImapMailbox(config, connect=boom).fetch_raw_since(SINCE))


def test_search_uses_day_granularity():
    server = FakeIMAP(mails={"1": build_mail()})
    list(mailbox(server).fetch_raw_since(datetime(2026, 9, 7, tzinfo=UTC)))
    search = next(c for c in server.commands if c[0] == "SEARCH")
    assert search[-1] == "07-Sep-2026"


def test_adapter_yields_normalized_events():
    server = FakeIMAP(mails={"1": build_mail(subject="报销单已通过")})
    events = list(EmailAdapter(mailbox(server)).fetch(SINCE))
    assert len(events) == 1
    assert events[0].normalized.title == "报销单已通过"


def test_one_broken_mail_does_not_stop_the_round():
    server = FakeIMAP(
        mails={
            "1": b"\x00 garbage",
            "2": build_mail(subject="正常邮件"),
        }
    )
    events = list(EmailAdapter(mailbox(server)).fetch(SINCE))
    assert len(events) == 2
    assert events[0].failed is True
    assert events[1].normalized.title == "正常邮件"
