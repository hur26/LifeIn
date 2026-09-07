"""凭据加密的测试。

每一条对应一个具体的失败场景,不是为覆盖率凑数。
"""

import base64
import os

import pytest

from lifein.config import Settings
from lifein.crypto import DecryptError, decrypt, encrypt, key_version_of, needs_rotation
from tests.test_config import BASE

USER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**BASE, **overrides})


def test_roundtrip():
    s = settings(master_key=key())
    blob = encrypt("授权码-abc123", user_id=USER, kind="imap", settings=s)
    assert decrypt(blob, user_id=USER, kind="imap", settings=s) == "授权码-abc123"


def test_plaintext_never_appears_in_ciphertext():
    s = settings(master_key=key())
    blob = encrypt("授权码-abc123", user_id=USER, kind="imap", settings=s)
    assert "授权码-abc123".encode() not in blob


def test_ciphertext_cannot_be_moved_to_another_user():
    # AAD 绑定 user_id:把别人那行的密文复制到自己行上,解不开
    s = settings(master_key=key())
    blob = encrypt("授权码", user_id=USER, kind="imap", settings=s)
    with pytest.raises(DecryptError):
        decrypt(blob, user_id=OTHER, kind="imap", settings=s)


def test_ciphertext_cannot_be_moved_to_another_kind():
    s = settings(master_key=key())
    blob = encrypt("授权码", user_id=USER, kind="imap", settings=s)
    with pytest.raises(DecryptError):
        decrypt(blob, user_id=USER, kind="collector", settings=s)


def test_tampering_is_detected():
    # GCM 自带完整性校验:改一个字节就该解密失败,而不是给出一段垃圾明文
    s = settings(master_key=key())
    blob = bytearray(encrypt("授权码", user_id=USER, kind="imap", settings=s))
    blob[-1] ^= 0x01
    with pytest.raises(DecryptError):
        decrypt(bytes(blob), user_id=USER, kind="imap", settings=s)


def test_not_our_envelope_is_rejected():
    s = settings(master_key=key())
    with pytest.raises(DecryptError):
        decrypt(b"XX" + os.urandom(40), user_id=USER, kind="imap", settings=s)


class TestRotation:
    """轮换是这个模块唯一复杂的地方,单独一组。"""

    def setup_method(self):
        self.old, self.new = key(), key()
        self.v1 = settings(master_key=self.old, master_key_version=1)
        self.blob = encrypt("旧钥匙加的", user_id=USER, kind="imap", settings=self.v1)

    def test_old_ciphertext_still_readable_during_rotation(self):
        v2 = settings(master_key=self.new, master_key_version=2, master_key_previous=self.old)
        assert decrypt(self.blob, user_id=USER, kind="imap", settings=v2) == "旧钥匙加的"

    def test_half_done_rotation_fails_loudly(self):
        # 换了主密钥却没配 MASTER_KEY_PREVIOUS —— 必须让人看见,不能静默失败
        v2 = settings(master_key=self.new, master_key_version=2)
        with pytest.raises(DecryptError) as exc:
            decrypt(self.blob, user_id=USER, kind="imap", settings=v2)
        assert "MASTER_KEY_PREVIOUS" in str(exc.value)

    def test_rotation_scan_finds_stale_ciphertext(self):
        v2 = settings(master_key=self.new, master_key_version=2, master_key_previous=self.old)
        assert key_version_of(self.blob) == 1
        assert needs_rotation(self.blob, v2) is True

        rewrapped = encrypt("旧钥匙加的", user_id=USER, kind="imap", settings=v2)
        assert needs_rotation(rewrapped, v2) is False

    def test_new_data_always_uses_current_key(self):
        v2 = settings(master_key=self.new, master_key_version=2, master_key_previous=self.old)
        assert key_version_of(encrypt("新的", user_id=USER, kind="imap", settings=v2)) == 2
