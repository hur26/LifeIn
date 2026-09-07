"""IMAP 收件。

ADR-011 把数据源全面本地化到 QQ / 163 / 126,于是要吃它们各自的怪脾气:

**163 / 126 必须在 SELECT 之前先发 `ID` 命令**,否则服务端回 `Unsafe Login`。
Python 的 `imaplib` 默认不允许在已认证状态发 ID,得先把它登记成 AUTH 态可用命令。
QQ 邮箱没有这个要求(07 §3)。

**用的是授权码不是登录密码。** 授权码会在账号密码变更后失效,所以认证失败
必须告警而不是静默停采(R9)—— 静默停采的表现是"提醒悄悄变少",
半个月都发现不了。

**取信时用 BODY.PEEK 而不是 RFC822。** 后者会把邮件标成已读 ——
系统读了你的邮箱,不该改变你自己看到的样子。这是只读接入的最基本礼貌。
"""

from __future__ import annotations

import imaplib
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)

# 网易系需要 ID 握手。用后缀匹配,免得漏掉 imap.yeah.net 这种同一套后端的域名。
_NEEDS_ID_HANDSHAKE = ("163.com", "126.com", "yeah.net")

_ID_ARGS = '("name" "lifein" "version" "0.0.1" "vendor" "selfhosted")'


class ImapError(RuntimeError):
    """IMAP 侧的失败。子类区分"该告警"和"该重试"。"""


class ImapAuthFailed(ImapError):
    """认证失败。**必须告警**:授权码多半是在改账号密码时失效的,
    而它的表现只是提醒悄悄变少,不告警半个月都发现不了。"""


class ImapUnavailable(ImapError):
    """连不上或服务端拒绝。可重试,重试仍失败再告警。"""


@dataclass(frozen=True)
class ImapConfig:
    host: str
    username: str
    auth_code: str
    """**授权码,不是登录密码。** 存 credentials 表加密字段,不进环境变量(07 §3)。"""

    port: int = 993
    mailbox: str = "INBOX"

    @property
    def needs_id_handshake(self) -> bool:
        return self.host.lower().endswith(_NEEDS_ID_HANDSHAKE)


def _default_connect(config: ImapConfig) -> imaplib.IMAP4:
    return imaplib.IMAP4_SSL(config.host, config.port)


class ImapMailbox:
    """一次会话。用完即关,不做长连接 —— P0 每天只跑几次,连接池是负担不是收益。"""

    def __init__(
        self,
        config: ImapConfig,
        *,
        connect: Callable[[ImapConfig], imaplib.IMAP4] | None = None,
    ) -> None:
        self._config = config
        self._connect = connect or _default_connect

    def fetch_raw_since(self, since: datetime) -> Iterator[tuple[str, bytes]]:
        """取 `since` 当天及之后的邮件,产出 (uid, 原始字节)。

        IMAP 的 SEARCH SINCE 只精确到**天**,所以这里会多取一些。
        不在这里按小时过滤:归一化之后按 Date 头判断更准,而重复取到的邮件
        会被 (user_id, source, external_id) 唯一键挡掉(base.py 里那条约定)。
        """
        conn = self._open()
        try:
            criterion = since.strftime("%d-%b-%Y")
            status, data = conn.uid("SEARCH", None, "SINCE", criterion)
            if status != "OK":
                raise ImapUnavailable(f"SEARCH 失败:{status}")

            uids = (data[0] or b"").split()
            log.info("IMAP %s 命中 %d 封(SINCE %s)", self._config.host, len(uids), criterion)

            for uid in uids:
                # BODY.PEEK[] 而不是 RFC822:不把邮件标成已读
                status, payload = conn.uid("FETCH", uid.decode(), "(BODY.PEEK[])")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    # 单封取失败不该中断整轮 —— 下一轮 SINCE 还会再命中它
                    log.warning("取信失败,跳过 uid=%s", uid)
                    continue
                yield uid.decode(), payload[0][1]
        finally:
            self._close(conn)

    # ---------- 连接 ----------

    def _open(self) -> imaplib.IMAP4:
        try:
            conn = self._connect(self._config)
        except OSError as exc:
            raise ImapUnavailable(f"连不上 {self._config.host}:{exc}") from exc

        try:
            conn.login(self._config.username, self._config.auth_code)
        except imaplib.IMAP4.error as exc:
            raise ImapAuthFailed(
                f"{self._config.username} 认证失败 —— 授权码可能在改账号密码时失效了:{exc}"
            ) from exc

        if self._config.needs_id_handshake:
            self._send_id(conn)

        status, _ = conn.select(self._config.mailbox, readonly=True)
        if status != "OK":
            raise ImapUnavailable(f"打不开邮箱 {self._config.mailbox}:{status}")
        return conn

    @staticmethod
    def _send_id(conn: imaplib.IMAP4) -> None:
        """网易系的 ID 握手。

        `imaplib` 不认识 ID 命令,要先把它登记成认证态可用,再走内部的
        `_simple_command`。这是官方 imaplib 没覆盖的场景,只能这么绕。
        """
        imaplib.Commands["ID"] = ("AUTH",)  # type: ignore[assignment]
        try:
            conn._simple_command("ID", _ID_ARGS)  # noqa: SLF001
            conn._untagged_response("OK", [None], "ID")  # noqa: SLF001
        except imaplib.IMAP4.error as exc:
            raise ImapUnavailable(f"ID 握手失败(163/126 必须先发 ID):{exc}") from exc

    @staticmethod
    def _close(conn: imaplib.IMAP4) -> None:
        # 关连接失败不影响已经取到的数据,记一行就够,不要覆盖掉真正的异常
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            log.debug("logout 失败,忽略")
