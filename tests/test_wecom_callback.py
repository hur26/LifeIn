"""企微回调的测试。

这是本项目唯一对公网开放的入口,所以用例的重心不是"能不能解开",
而是**该拒的有没有拒**:签名不对、时间戳过期、别人企业的密文、超大 body。

测试自己实现一遍加密,而不是把密文写死 —— 写死的密文在改动实现时只会
一起改错,自己加密才能证明两边真的对得上。
"""

from __future__ import annotations

import base64
import os
import struct
import time

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from lifein.channels.wecom_callback import (
    MAX_BODY_BYTES,
    CallbackRejected,
    WecomCallback,
    compute_signature,
)

TOKEN = "my-token"
CORP_ID = "ww1234567890"
AES_KEY = base64.b64encode(os.urandom(32)).decode()[:43]  # 企微给的就是 43 个字符

MESSAGE_XML = (
    "<xml><ToUserName><![CDATA[ww123]]></ToUserName>"
    "<FromUserName><![CDATA[BaiYang]]></FromUserName>"
    "<CreateTime>1788764400</CreateTime>"
    "<MsgType><![CDATA[text]]></MsgType>"
    "<Content><![CDATA[上周我答应了谁什么事]]></Content>"
    "<MsgId>1234567890123456</MsgId>"
    "<AgentID>1000002</AgentID></xml>"
)


def encrypt_for(plain: str, *, corp_id: str = CORP_ID, aes_key: str = AES_KEY) -> str:
    """按企微的格式加密:16 字节随机 + 4 字节长度 + 正文 + corpid,PKCS7 补到 32。"""
    key = base64.b64decode(aes_key + "=")
    body = plain.encode()
    packed = os.urandom(16) + struct.pack(">I", len(body)) + body + corp_id.encode()
    pad = 32 - len(packed) % 32
    packed += bytes([pad]) * pad

    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(packed) + encryptor.finalize()).decode()


def signed(encrypt: str, timestamp: str, nonce: str = "n1") -> dict:
    return {
        "msg_signature": compute_signature(TOKEN, timestamp, nonce, encrypt),
        "timestamp": timestamp,
        "nonce": nonce,
    }


def callback(**kw) -> WecomCallback:
    return WecomCallback(token=TOKEN, aes_key=AES_KEY, corp_id=CORP_ID, **kw)


def body_for(encrypt: str) -> bytes:
    return (
        f"<xml><ToUserName>ww123</ToUserName><Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>"
    ).encode()


NOW = 1788764400.0


def test_url_verification_returns_the_plaintext():
    encrypt = encrypt_for("echo-me-back")
    assert callback().verify_url(echostr=encrypt, **signed(encrypt, "1788764400")) == "echo-me-back"


def test_message_is_parsed():
    encrypt = encrypt_for(MESSAGE_XML)
    msg = callback().parse_message(
        body=body_for(encrypt), now=NOW, **signed(encrypt, str(int(NOW)))
    )
    assert msg.sender == "BaiYang"
    assert msg.msg_type == "text"
    assert msg.content == "上周我答应了谁什么事"
    assert msg.msg_id == "1234567890123456"


class TestRejections:
    def test_wrong_signature(self):
        encrypt = encrypt_for(MESSAGE_XML)
        params = signed(encrypt, str(int(NOW)))
        params["msg_signature"] = "0" * 40
        with pytest.raises(CallbackRejected):
            callback().parse_message(body=body_for(encrypt), now=NOW, **params)

    def test_missing_signature(self):
        encrypt = encrypt_for(MESSAGE_XML)
        params = signed(encrypt, str(int(NOW)))
        params["msg_signature"] = ""
        with pytest.raises(CallbackRejected):
            callback().parse_message(body=body_for(encrypt), now=NOW, **params)

    def test_tampered_body_breaks_the_signature(self):
        encrypt = encrypt_for(MESSAGE_XML)
        params = signed(encrypt, str(int(NOW)))
        other = encrypt_for("<xml><FromUserName>Attacker</FromUserName></xml>")
        with pytest.raises(CallbackRejected):
            callback().parse_message(body=body_for(other), now=NOW, **params)

    def test_stale_timestamp_is_a_replay(self):
        encrypt = encrypt_for(MESSAGE_XML)
        old = str(int(NOW - 3600))
        with pytest.raises(CallbackRejected) as exc:
            callback().parse_message(body=body_for(encrypt), now=NOW, **signed(encrypt, old))
        assert "重放" in str(exc.value)

    def test_future_timestamp_is_also_rejected(self):
        encrypt = encrypt_for(MESSAGE_XML)
        future = str(int(NOW + 3600))
        with pytest.raises(CallbackRejected):
            callback().parse_message(body=body_for(encrypt), now=NOW, **signed(encrypt, future))

    def test_another_companys_ciphertext(self):
        """密文里带着 corpid。别人拿走回调地址也伪造不出这一段。"""
        encrypt = encrypt_for(MESSAGE_XML, corp_id="ww_someone_else")
        with pytest.raises(CallbackRejected) as exc:
            callback().parse_message(
                body=body_for(encrypt), now=NOW, **signed(encrypt, str(int(NOW)))
            )
        assert "receiveid" in str(exc.value)

    def test_oversized_body_is_rejected_before_parsing(self):
        huge = b"<xml>" + b"x" * MAX_BODY_BYTES + b"</xml>"
        with pytest.raises(CallbackRejected) as exc:
            callback().parse_message(
                body=huge, msg_signature="s", timestamp=str(int(NOW)), nonce="n", now=NOW
            )
        assert "字节" in str(exc.value)

    def test_body_without_encrypt_field(self):
        with pytest.raises(CallbackRejected):
            callback().parse_message(
                body=b"<xml><Hello>1</Hello></xml>",
                msg_signature="s",
                timestamp=str(int(NOW)),
                nonce="n",
                now=NOW,
            )

    def test_garbage_ciphertext(self):
        encrypt = base64.b64encode(b"not-really-encrypted-but-32-bytes").decode()
        with pytest.raises(CallbackRejected):
            callback().parse_message(
                body=body_for(encrypt), now=NOW, **signed(encrypt, str(int(NOW)))
            )

    def test_non_integer_timestamp(self):
        encrypt = encrypt_for(MESSAGE_XML)
        params = signed(encrypt, "昨天")
        with pytest.raises(CallbackRejected):
            callback().parse_message(body=body_for(encrypt), now=NOW, **params)


def test_entity_expansion_payload_does_not_get_parsed_as_xml():
    """签名校验之前解析的是未经认证的 XML —— 所以我们根本不解析它。

    这个 body 对 XML 解析器是一颗炸弹,对正则只是一段没有 Encrypt 的文本。
    """
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">]><xml>&lol2;</xml>'
    )
    with pytest.raises(CallbackRejected):
        callback().parse_message(
            body=bomb, msg_signature="s", timestamp=str(int(NOW)), nonce="n", now=NOW
        )


def test_bad_aes_key_length_fails_at_construction():
    # 配置错了要在启动时炸,不要等第一条回调进来
    with pytest.raises(ValueError):
        WecomCallback(token=TOKEN, aes_key=base64.b64encode(b"short").decode(), corp_id=CORP_ID)


def test_signature_algorithm_matches_the_spec():
    # 企微定的:四个值排序后拼接取 SHA1
    import hashlib

    expected = hashlib.sha1("".join(sorted(["t", "123", "n", "e"])).encode()).hexdigest()
    assert compute_signature("t", "123", "n", "e") == expected


def test_default_skew_window_is_five_minutes():
    encrypt = encrypt_for(MESSAGE_XML)
    within = str(int(NOW - 299))
    assert (
        callback().parse_message(body=body_for(encrypt), now=NOW, **signed(encrypt, within)).sender
        == "BaiYang"
    )

    outside = str(int(NOW - 301))
    with pytest.raises(CallbackRejected):
        callback().parse_message(body=body_for(encrypt), now=NOW, **signed(encrypt, outside))


def test_real_clock_is_used_when_now_is_not_given():
    encrypt = encrypt_for(MESSAGE_XML)
    ts = str(int(time.time()))
    assert (
        callback().parse_message(body=body_for(encrypt), **signed(encrypt, ts)).msg_type == "text"
    )
