"""签名与 token 的纯函数(06 §6.2 / §6.3)。不需要数据库,也不起 HTTP。

**第一组是跨端一致性的黄金向量**:安卓端有一份一模一样的用例
(`android/app/src/test/java/ltd/iclab/lifein/SigningTest.kt`)。
同一组值两端各测一次 —— 这是唯一能在不联网的情况下发现"两边算得不一样"的办法,
而那种不一样的表现是所有请求都 401、且服务端按设计不说原因。

改签名格式时两边的用例会同时红。那正是要的效果。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lifein.api import auth

SECRET = "bGlmZWluLXRlc3Qtc2VjcmV0LWtleS0zMi1ieXRlcyE="
BODY = b'{"device_id":"pixel-7a","events":[]}'
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


class TestGoldenVector:
    def test_signing_string_shape(self):
        message = auth.signing_string(
            method="POST", path="/ingest/events", timestamp="1788768000", body=BODY
        )
        assert message.decode() == (
            "POST\n/ingest/events\n1788768000\n"
            "583c733aa7218d83853381698672eaf8e8091e50eff2740850aa47100e8843dc"
        )

    def test_signature_value(self):
        message = auth.signing_string(
            method="POST", path="/ingest/events", timestamp="1788768000", body=BODY
        )
        assert (
            auth.sign(SECRET, message)
            == "b91d913afc346c404c453bfb44154c134fa2d4464c6e930cd626f35514b65858"
        )

    def test_method_is_upper_cased_and_empty_body_still_has_a_digest(self):
        # 空 body 也要有摘要:少了它,一次心跳能被改成一次上报
        message = auth.signing_string(
            method="post", path="/ingest/heartbeat", timestamp="1", body=b""
        )
        assert message.decode() == (
            "POST\n/ingest/heartbeat\n1\n"
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )


class TestSignature:
    def test_verify_accepts_its_own_signature(self):
        message = auth.signing_string(
            method="POST", path="/ingest/events", timestamp="1788768000", body=BODY
        )
        assert auth.verify(SECRET, message, auth.sign(SECRET, message))

    def test_any_changed_byte_breaks_it(self):
        original = auth.signing_string(
            method="POST", path="/ingest/events", timestamp="1788768000", body=BODY
        )
        signature = auth.sign(SECRET, original)

        # 改路径:一次心跳被改成一次上报
        moved = auth.signing_string(
            method="POST", path="/ingest/heartbeat", timestamp="1788768000", body=BODY
        )
        assert not auth.verify(SECRET, moved, signature)

        # 改 body:上报的内容被整个换掉
        tampered = auth.signing_string(
            method="POST", path="/ingest/events", timestamp="1788768000", body=BODY + b" "
        )
        assert not auth.verify(SECRET, tampered, signature)

    def test_a_bad_secret_is_reported_as_such(self):
        with pytest.raises(auth.AuthError):
            auth.sign("这不是 base64", b"x")


class TestTimestamp:
    def test_within_the_window(self):
        assert auth.timestamp_is_fresh(
            str(int(NOW.timestamp())), now=NOW, max_skew_s=300
        )

    def test_too_old(self):
        old = str(int((NOW - timedelta(minutes=10)).timestamp()))
        assert not auth.timestamp_is_fresh(old, now=NOW, max_skew_s=300)

    def test_too_new(self):
        """只卡"太旧"的话,把时钟调到明年就能签出永远新鲜的请求。"""
        ahead = str(int((NOW + timedelta(minutes=10)).timestamp()))
        assert not auth.timestamp_is_fresh(ahead, now=NOW, max_skew_s=300)

    def test_garbage_is_not_fresh(self):
        assert not auth.timestamp_is_fresh("昨天", now=NOW, max_skew_s=300)
        assert not auth.timestamp_is_fresh("", now=NOW, max_skew_s=300)


class TestToken:
    def token(self, *, expires_in_h: int = 24) -> str:
        return auth.mint_token(
            secret=SECRET,
            user_id="u-1",
            device_id="pixel-7a",
            expires_at=NOW + timedelta(hours=expires_in_h),
        )

    def test_roundtrip(self):
        payload = auth.verify_token(self.token(), secret=SECRET, now=NOW)
        assert (payload.user_id, payload.device_id) == ("u-1", "pixel-7a")

    def test_parse_works_before_verification(self):
        """先解出是谁,才知道该去库里取哪把密钥 —— 解出来的东西验签之前不能信。"""
        payload = auth.parse_token(self.token())
        assert payload.device_id == "pixel-7a"

    def test_another_secret_cannot_forge_one(self):
        """拿采集密钥伪造的 token 进不来 —— R11 那句"最重要的一条"。"""
        other = "b3RoZXItc2VjcmV0LWtleS0zMi1ieXRlcy1sb25nISE="
        forged = auth.mint_token(
            secret=other, user_id="u-1", device_id="pixel-7a", expires_at=NOW + timedelta(hours=1)
        )
        with pytest.raises(auth.AuthError):
            auth.verify_token(forged, secret=SECRET, now=NOW)

    def test_expired(self):
        with pytest.raises(auth.AuthError):
            auth.verify_token(self.token(expires_in_h=-1), secret=SECRET, now=NOW)

    def test_tampered_payload_is_rejected(self):
        head, payload, signature = self.token().split(".")
        with pytest.raises(auth.AuthError):
            auth.verify_token(f"{head}.{payload[:-2]}xx.{signature}", secret=SECRET, now=NOW)

    def test_shapes_that_are_not_tokens(self):
        for bad in ("", "abc", "v2.x.y", "v1.notbase64.zz"):
            with pytest.raises(auth.AuthError):
                auth.verify_token(bad, secret=SECRET, now=NOW)
