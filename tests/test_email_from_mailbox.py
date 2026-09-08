"""邮件通道从**已有的邮箱凭据**派生(不再要求把授权码抄进 .env)。

为什么这么改:QQ / 163 / 126 的 IMAP 与 SMTP **是同一个授权码**,而它已经
加密躺在 `credentials` 里。再要人往 `.env` 里抄一份明文,正好撞上
[07 §1](../docs/07-config.md) 那句"最容易做错的是把 IMAP 授权码写进环境变量",
也和 [ADR-009](../docs/04-tech-decisions.md) 冲突 ——
**同一份秘密不该同时存在于加密的库里和明文的文件里。**

这一组盯两件事:主机名推得对不对(推错的表现是每次告警都失败,
而告警失败本身不会被告警),以及**推不出来时不要瞎猜**。
"""

from __future__ import annotations

import pytest

from lifein.bootstrap import _build_email_channel, _smtp_from_mailbox, _smtp_host_for
from lifein.config import Settings
from lifein.repos import credentials
from tests.test_config import BASE


class TestHostMapping:
    """纯函数,不需要库。"""

    @pytest.mark.parametrize(
        ("imap_host", "expected"),
        [
            ("imap.qq.com", "smtp.qq.com"),
            ("imap.163.com", "smtp.163.com"),
            ("imap.126.com", "smtp.126.com"),
            ("IMAP.QQ.COM", "smtp.qq.com"),
            ("  imap.qq.com  ", "smtp.qq.com"),
        ],
    )
    def test_imap_host_becomes_smtp_host(self, imap_host, expected):
        assert _smtp_host_for(imap_host) == expected

    @pytest.mark.parametrize("imap_host", ["mail.example.com", "example.com", "", "smtp.qq.com"])
    def test_unknown_shapes_are_refused_not_guessed(self, imap_host):
        """推不出来就不启用。

        一个连不上的发信主机会让降级链多出一条**必然失败**的通道,
        而那条通道正是告警的出口 —— 它坏了没有任何人会告诉你。
        """
        assert _smtp_host_for(imap_host) is None


@pytest.mark.integration
class TestDerivingFromTheStoredCredential:
    def settings(self) -> Settings:
        return Settings(_env_file=None, **BASE)

    def store_imap(self, session, user_id, *, host="imap.qq.com", username="me@qq.com"):
        credentials.put_credential(
            user_id,
            session,
            kind="imap",
            scope="query",
            payload={"host": host, "username": username, "auth_code": "16位授权码", "port": 993},
            settings=self.settings(),
        )

    def test_channel_comes_up_without_any_smtp_env(self, pg_session, user_id, monkeypatch):
        s = self.settings()
        assert not s.smtp_enabled  # 前提:环境变量里什么都没配

        self.store_imap(pg_session, user_id)
        monkeypatch.setattr("lifein.bootstrap._first_active_user", lambda: user_id)
        monkeypatch.setattr("lifein.bootstrap.session_scope", lambda: _Scope(pg_session))

        config = _smtp_from_mailbox(s)
        assert config is not None
        assert (config.host, config.port, config.use_ssl) == ("smtp.qq.com", 465, True)
        assert config.username == config.sender == "me@qq.com"
        # 密码就是那把授权码,从加密的库里解出来的 —— 不经过 .env
        assert config.password == "16位授权码"

        channel = _build_email_channel(s)
        assert channel is not None
        # 没配收件人就发给自己:告警要落在你天天看的那个邮箱里。
        # 不会回环 —— 发出去的信带 X-LifeIn-Push,采集侧见到就跳过
        assert channel._resolve(user_id) == "me@qq.com"

    def test_an_unrecognised_mail_host_disables_the_channel(
        self, pg_session, user_id, monkeypatch
    ):
        self.store_imap(pg_session, user_id, host="mail.mycompany.com")
        monkeypatch.setattr("lifein.bootstrap._first_active_user", lambda: user_id)
        monkeypatch.setattr("lifein.bootstrap.session_scope", lambda: _Scope(pg_session))

        assert _smtp_from_mailbox(self.settings()) is None

    def test_env_wins_when_both_exist(self, pg_session, user_id, monkeypatch):
        """专门配一个发信邮箱(不被采集的那个)时,环境变量优先。"""
        self.store_imap(pg_session, user_id)
        monkeypatch.setattr("lifein.bootstrap._first_active_user", lambda: user_id)
        monkeypatch.setattr("lifein.bootstrap.session_scope", lambda: _Scope(pg_session))

        s = Settings(
            _env_file=None,
            **{
                **BASE,
                "smtp_host": "smtp.other.com",
                "smtp_username": "alerts@other.com",
                "smtp_password": "另一把",
                "smtp_to": "me@qq.com",
            },
        )
        channel = _build_email_channel(s)
        assert channel is not None
        assert channel._from == "alerts@other.com"
        assert channel._resolve(user_id) == "me@qq.com"

    def test_no_credential_at_all_means_no_channel(self, pg_session, user_id, monkeypatch):
        monkeypatch.setattr("lifein.bootstrap._first_active_user", lambda: user_id)
        monkeypatch.setattr("lifein.bootstrap.session_scope", lambda: _Scope(pg_session))

        assert _build_email_channel(self.settings()) is None


class _Scope:
    """把测试那个事务包成 `session_scope()` 的形状。"""

    def __init__(self, session) -> None:
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *exc):
        return False
