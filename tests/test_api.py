"""HTTP 入口的测试。

用 FastAPI 的 TestClient,不起真服务。被测的是端点层自己的决定:
拒绝时不泄露原因、处理失败也回 200、健康检查不需要任何依赖。
"""

from __future__ import annotations

import base64
import os

import pytest
from fastapi.testclient import TestClient

from lifein.api.app import create_app
from lifein.bootstrap import Services
from lifein.channels.wecom_callback import WecomCallback
from tests.test_wecom_callback import (
    MESSAGE_XML,
    TOKEN,
    body_for,
    compute_signature,
    encrypt_for,
)
from tests.test_wecom_callback import AES_KEY as CB_AES_KEY
from tests.test_wecom_callback import CORP_ID as CB_CORP_ID


class Recorder:
    def __init__(self) -> None:
        self.handled: list = []


@pytest.fixture
def recorder(monkeypatch) -> Recorder:
    """替掉 handle_message —— 端点层的职责到"解出消息"为止。"""
    rec = Recorder()

    def fake_handle(session, *, message, deps, now):
        rec.handled.append(message)

    monkeypatch.setattr("lifein.api.app.handle_message", fake_handle)
    monkeypatch.setattr("lifein.api.app.session_scope", _null_session_scope)
    return rec


class _NullSession:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _null_session_scope():
    return _NullSession()


@pytest.fixture
def client() -> TestClient:
    services = Services(
        settings=None,  # 端点层用不到,别为了测试去造一份完整配置
        llm=None,
        wecom=None,
        channel=None,
        callback=WecomCallback(token=TOKEN, aes_key=CB_AES_KEY, corp_id=CB_CORP_ID),
        alerter=None,
    )
    return TestClient(create_app(services))


def signed_params(encrypt: str, timestamp: str) -> dict:
    return {
        "msg_signature": compute_signature(TOKEN, timestamp, "n1", encrypt),
        "timestamp": timestamp,
        "nonce": "n1",
    }


def test_healthz_needs_nothing(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_url_verification_echoes_the_plaintext(client):
    import time

    encrypt = encrypt_for("echo-me")
    params = signed_params(encrypt, str(int(time.time())))
    response = client.get("/wecom/callback", params={**params, "echostr": encrypt})

    assert response.status_code == 200
    assert response.text == "echo-me"


def test_bad_signature_gets_a_bare_400(client):
    """告诉对方错在哪一步,等于帮他调试。"""
    encrypt = encrypt_for("echo-me")
    response = client.get(
        "/wecom/callback",
        params={
            "msg_signature": "0" * 40,
            "timestamp": "1788764400",
            "nonce": "n1",
            "echostr": encrypt,
        },
    )
    assert response.status_code == 400
    assert response.text == ""


def test_message_reaches_the_handler(client, recorder):
    import time

    encrypt = encrypt_for(MESSAGE_XML)
    params = signed_params(encrypt, str(int(time.time())))
    response = client.post("/wecom/callback", params=params, content=body_for(encrypt))

    assert response.status_code == 200
    assert recorder.handled[0].content == "上周我答应了谁什么事"


def test_empty_body_is_returned_so_wecom_does_not_echo(client, recorder):
    # 真正的回复是我们主动 send 出去的,不走这个响应体
    import time

    encrypt = encrypt_for(MESSAGE_XML)
    params = signed_params(encrypt, str(int(time.time())))
    assert client.post("/wecom/callback", params=params, content=body_for(encrypt)).text == ""


def test_handler_failure_still_returns_200(client, monkeypatch):
    """企微对非 200 会重投,而重投意味着再花一次模型钱、再回一次消息。"""
    import time

    def boom(*args, **kwargs):
        raise RuntimeError("模型挂了")

    monkeypatch.setattr("lifein.api.app.handle_message", boom)
    monkeypatch.setattr("lifein.api.app.session_scope", _null_session_scope)

    encrypt = encrypt_for(MESSAGE_XML)
    params = signed_params(encrypt, str(int(time.time())))
    assert (
        client.post("/wecom/callback", params=params, content=body_for(encrypt)).status_code == 200
    )


def test_rejected_post_is_not_passed_to_the_handler(client, recorder):
    encrypt = encrypt_for(MESSAGE_XML)
    response = client.post(
        "/wecom/callback",
        params={"msg_signature": "0" * 40, "timestamp": "1788764400", "nonce": "n1"},
        content=body_for(encrypt),
    )
    assert response.status_code == 400
    assert recorder.handled == []


def test_docs_endpoints_are_disabled(client):
    # 只服务两个已知调用方,把 schema 挂在公网上是白送的侦察信息
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_aes_key_from_env_shape_is_accepted():
    # 企微给的是 43 个字符,末尾少一个 '='
    key = base64.b64encode(os.urandom(32)).decode()[:43]
    WecomCallback(token="t", aes_key=key, corp_id="ww1")
