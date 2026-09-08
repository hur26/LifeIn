"""iLink 扫码登录的测试。

状态机有五个状态,漏处理任何一个的表现都是"扫了没反应"。其中
`scaned_but_redirect` 最阴险:漏了会一直用旧地址轮询,永远等不到 confirmed,
而日志上看不出任何异常 —— 所以它有专门一条用例。
"""

from __future__ import annotations

import httpx
import pytest

from lifein.channels.weixin_login import LoginFailed, QrCode, fetch_qr, login

BASE = "https://ilink.example.com"

QR_RESPONSE = {"qrcode": "abc123", "qrcode_img_content": "https://weixin.qq.com/x/abc"}


def build(statuses: list[dict], *, qr_responses: list[dict] | None = None):
    """statuses 按顺序返回;qr_responses 用于二维码刷新。"""
    qrs = list(qr_responses or [QR_RESPONSE])
    seen: list[httpx.Request] = []
    remaining = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "get_bot_qrcode" in request.url.path:
            return httpx.Response(200, json=qrs.pop(0) if len(qrs) > 1 else qrs[0])
        return httpx.Response(200, json=remaining.pop(0) if remaining else {"status": "wait"})

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def run(statuses, **kw):
    client, seen = build(statuses, qr_responses=kw.pop("qr_responses", None))
    shown: list[QrCode] = []
    scanned: list[bool] = []
    result = login(
        client,
        show_qr=shown.append,
        on_scanned=lambda: scanned.append(True),
        base_url=BASE,
        sleep=lambda _s: None,
        **kw,
    )
    return result, seen, shown, scanned


CONFIRMED = {
    "status": "confirmed",
    "ilink_bot_id": "bot-1",
    "bot_token": "tok-1",
    "baseurl": "https://ilink-2.example.com",
    "ilink_user_id": "me-1",
}


def test_happy_path():
    result, _, shown, _ = run([CONFIRMED])
    assert result.account_id == "bot-1"
    assert result.token == "tok-1"
    assert result.base_url == "https://ilink-2.example.com"
    assert shown[0].url == "https://weixin.qq.com/x/abc"


def test_waits_then_confirms():
    result, _, _, _ = run([{"status": "wait"}, {"status": "wait"}, CONFIRMED])
    assert result.token == "tok-1"


def test_scanned_notifies_once():
    """扫完到点确认之间会轮询很多轮,每轮都喊一遍很吵。"""
    _, _, _, scanned = run([{"status": "scaned"}, {"status": "scaned"}, CONFIRMED])
    assert len(scanned) == 1


def test_redirect_switches_the_base_url():
    """漏掉这条会一直用旧地址轮询,永远等不到 confirmed。"""
    _, seen, _, _ = run(
        [{"status": "scaned_but_redirect", "redirect_host": "ilink-9.example.com"}, CONFIRMED]
    )
    status_calls = [r for r in seen if "get_qrcode_status" in r.url.path]
    assert status_calls[0].url.host == "ilink.example.com"
    assert status_calls[1].url.host == "ilink-9.example.com"  # 换过去了


def test_expired_qr_is_refreshed():
    second_qr = {"qrcode": "def456", "qrcode_img_content": "https://weixin.qq.com/x/def"}
    _, _, shown, _ = run(
        [{"status": "expired"}, CONFIRMED], qr_responses=[QR_RESPONSE, second_qr]
    )
    assert len(shown) == 2
    assert shown[1].value == "def456"


def test_too_many_expirations_gives_up():
    with pytest.raises(LoginFailed) as exc:
        run([{"status": "expired"}] * 5)
    assert "过期" in str(exc.value)


def test_timeout():
    ticks = iter([0, 1, 2, 999])
    with pytest.raises(LoginFailed) as exc:
        run([{"status": "wait"}] * 10, timeout_s=10, now=lambda: next(ticks))
    assert "扫码" in str(exc.value)


def test_confirmed_without_credentials_is_an_error():
    """宁可报错也不要存一份用不了的凭据 —— 那会让后面每次推送都失败,
    而你以为已经配好了。"""
    with pytest.raises(LoginFailed):
        run([{"status": "confirmed", "ilink_bot_id": "bot-1"}])  # 没有 token


def test_network_hiccups_do_not_abort_the_login():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "get_bot_qrcode" in request.url.path:
            return httpx.Response(200, json=QR_RESPONSE)
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("blip", request=request)
        return httpx.Response(200, json=CONFIRMED)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = login(client, show_qr=lambda _q: None, base_url=BASE, sleep=lambda _s: None)
    assert result.token == "tok-1"


def test_qr_response_without_code_fails_fast():
    client, _ = build([], qr_responses=[{"qrcode": ""}])
    with pytest.raises(LoginFailed):
        fetch_qr(client, base_url=BASE)
