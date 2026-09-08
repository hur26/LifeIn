"""iLink 扫码登录。

流程只有三步:取二维码 → 轮询状态 → 拿到凭据。但状态机有五个状态,
每个都得处理对,否则表现是"扫了没反应"——最难查的那种。

| 状态 | 含义 | 怎么处理 |
| --- | --- | --- |
| `wait` | 还没扫 | 继续轮询 |
| `scaned` | 扫了,等手机上点确认 | **提示用户去点**,否则他会以为卡住了 |
| `scaned_but_redirect` | 换服务器 | **切到 `redirect_host` 再轮询** |
| `expired` | 二维码过期 | 重新取一个,最多三次 |
| `confirmed` | 成功 | 取出凭据 |

`scaned_but_redirect` 那条最容易漏:漏了就会一直用旧地址轮询,
永远等不到 `confirmed`,而日志上看不出任何异常。

**协议细节来自阅读 Hermes Agent 的 iLink 适配器**(MIT,NousResearch)。
实现是我们自己写的,没有复制代码 —— 协议本身不是谁的私产。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from lifein import qr as qr_render
from lifein.channels.weixin import APP_ID, BASE_URL, CLIENT_VERSION

log = logging.getLogger(__name__)

QR_ENDPOINT = "ilink/bot/get_bot_qrcode"
STATUS_ENDPOINT = "ilink/bot/get_qrcode_status"

DEFAULT_BOT_TYPE = "3"
POLL_INTERVAL_S = 1.0
DEFAULT_TIMEOUT_S = 480
MAX_QR_REFRESH = 3


class LoginFailed(RuntimeError):
    """扫码登录没成功。消息里说清是超时、过期还是响应不全。"""


@dataclass(frozen=True)
class LoginResult:
    account_id: str
    token: str
    base_url: str
    user_id: str
    """本次登录所用微信号在 iLink 里的 user id。

    **注意它不是推送目标。** 推送目标是"和 bot 对话的那个人",
    要等对方给 bot 发第一条消息才知道 —— 见 `weixin_inbound`。
    """


@dataclass(frozen=True)
class QrCode:
    value: str
    """轮询状态用的令牌。"""

    url: str
    """要让微信扫的那个链接。**扫 `value` 是没用的**,它只是个十六进制串。"""


def _headers() -> dict[str, str]:
    return {"iLink-App-Id": APP_ID, "iLink-App-ClientVersion": str(CLIENT_VERSION)}


def fetch_qr(
    client: httpx.Client, *, base_url: str = BASE_URL, bot_type: str = DEFAULT_BOT_TYPE
) -> QrCode:
    response = client.get(
        f"{base_url.rstrip('/')}/{QR_ENDPOINT}",
        params={"bot_type": bot_type},
        headers=_headers(),
    )
    response.raise_for_status()
    data = response.json()

    value = str(data.get("qrcode") or "")
    if not value:
        raise LoginFailed("二维码响应里没有 qrcode 字段")
    return QrCode(value=value, url=str(data.get("qrcode_img_content") or ""))


def login(
    client: httpx.Client,
    *,
    show_qr: Callable[[QrCode], None],
    on_scanned: Callable[[], None] | None = None,
    base_url: str = BASE_URL,
    bot_type: str = DEFAULT_BOT_TYPE,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> LoginResult:
    """走完扫码登录。`show_qr` 负责把二维码显示出来 —— 怎么显示不是这里的事。"""
    qr = fetch_qr(client, base_url=base_url, bot_type=bot_type)
    show_qr(qr)

    current_base = base_url
    refreshes = 0
    scanned_notified = False
    deadline = now() + timeout_s

    while now() < deadline:
        try:
            response = client.get(
                f"{current_base.rstrip('/')}/{STATUS_ENDPOINT}",
                params={"qrcode": qr.value},
                headers=_headers(),
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            # 轮询期间的网络抖动很常见,不该让整个登录失败
            log.debug("轮询二维码状态失败,继续:%s", type(exc).__name__)
            sleep(POLL_INTERVAL_S)
            continue

        status = str(data.get("status") or "wait")

        if status == "confirmed":
            return _to_result(data, fallback_base_url=current_base)

        if status == "scaned":
            if on_scanned and not scanned_notified:
                # 只提示一次。扫完到点确认之间会轮询很多轮,每轮都喊一遍很吵
                on_scanned()
                scanned_notified = True

        elif status == "scaned_but_redirect":
            redirect_host = str(data.get("redirect_host") or "")
            if redirect_host:
                # 漏掉这一条会一直用旧地址轮询,永远等不到 confirmed,
                # 而日志上看不出任何异常
                current_base = f"https://{redirect_host}"
                log.info("iLink 要求换到 %s 继续", redirect_host)

        elif status == "expired":
            refreshes += 1
            if refreshes > MAX_QR_REFRESH:
                raise LoginFailed(f"二维码连续 {MAX_QR_REFRESH} 次过期,请重新执行登录")
            log.info("二维码过期,换一个(%d/%d)", refreshes, MAX_QR_REFRESH)
            qr = fetch_qr(client, base_url=base_url, bot_type=bot_type)
            show_qr(qr)
            scanned_notified = False

        sleep(POLL_INTERVAL_S)

    raise LoginFailed(f"{timeout_s} 秒内没有完成扫码")


def _to_result(data: dict, *, fallback_base_url: str) -> LoginResult:
    account_id = str(data.get("ilink_bot_id") or "")
    token = str(data.get("bot_token") or "")
    if not account_id or not token:
        # 状态说成功但没给全 —— 宁可报错也不要存一份用不了的凭据,
        # 那会让后面每次推送都失败,而你以为已经配好了
        raise LoginFailed("iLink 返回 confirmed 但凭据不完整")

    return LoginResult(
        account_id=account_id,
        token=token,
        base_url=str(data.get("baseurl") or fallback_base_url),
        user_id=str(data.get("ilink_user_id") or ""),
    )


def write_qr_html(qr: QrCode, path: Path) -> None:
    """把登录二维码写成 HTML 文件。渲染本身在 `lifein.qr`,这里只管扫什么。

    **扫 `value` 是没用的**,要扫的是 `url` —— 这个区别是本函数存在的全部理由,
    别的调用方没有这层。
    """
    qr_render.write_qr_html(
        qr.url or qr.value,
        path,
        title="用微信扫这个二维码",
        hint="扫完在手机上点确认，然后在微信里给这个 bot 发一句话",
    )


def render_qr_ascii(qr: QrCode) -> str | None:
    """终端字符画版。装不了依赖就返回 None,由调用方退回打链接。"""
    return qr_render.render_qr_ascii(qr.url or qr.value)
