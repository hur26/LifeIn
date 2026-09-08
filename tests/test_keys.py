"""主密钥从哪来(P4 第 4 片)。不需要数据库。

**这一片做的是"让换 KMS 那次不需要动 `crypto.py`",不是那次升级本身。**
所以这一组测的也是那件事:密钥源换掉之后,加解密还走得通。

03 的 P4 硬门槛第 2 条要求"凭据加密从环境变量升级为 KMS 或等价方案",
而 ADR-022 把它从"以后再说"改成了更该早点做——因为 `.env` 会跟着磁盘快照
一起被复制。
"""

from __future__ import annotations

import base64
import os

import pytest

from lifein import crypto, keys
from lifein.config import Settings
from lifein.keys import EnvKeyProvider, KeyUnavailable
from tests.test_config import BASE


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**BASE, **overrides})


class _StubSecret:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


class _StubSettings:
    """绕开 `Settings` 的校验器,直接把一把坏密钥递给 provider。

    **不是为了测 provider 的健壮性本身** —— 是为了钉住"KMS 返回什么都要检"
    这件事:那条路上没有 pydantic 校验器。
    """

    master_key_version = 1
    master_key_previous = None

    def __init__(self, value: str) -> None:
        self.master_key = _StubSecret(value)


def _stub_settings(value: str) -> _StubSettings:
    return _StubSettings(value)


class TestTheEnvProvider:
    def test_it_hands_out_the_current_key(self):
        provider = EnvKeyProvider(settings())
        assert len(provider.key(provider.current_version)) == 32

    def test_an_unknown_version_without_a_previous_key_is_explicit(self):
        """**轮换做了一半必须让人看见。** 静默失败的表现是
        "搬完了但采不到邮件",而那时没人会想到是密钥。"""
        provider = EnvKeyProvider(settings())

        with pytest.raises(KeyUnavailable) as caught:
            provider.key(provider.current_version + 1)
        assert "MASTER_KEY_PREVIOUS" in str(caught.value)

    def test_a_short_key_is_refused(self):
        """**短了的密钥能正常加密,解密也正常** —— 只是强度不是你以为的那个,
        而那种问题不会有任何外部表现。

        `Settings` 在加载时已经校验过一遍,所以从配置来的密钥到不了这里 ——
        **这一道是给别的密钥源留的**:KMS 返回的东西没有经过那个校验器,
        而"KMS 配错了返回一把 16 字节的密钥"是完全可能的。
        """
        with pytest.raises(KeyUnavailable) as caught:
            EnvKeyProvider(_stub_settings(base64.b64encode(os.urandom(16)).decode())).key(1)
        assert "32" in str(caught.value)

    def test_a_non_base64_key_is_refused(self):
        with pytest.raises(KeyUnavailable):
            EnvKeyProvider(_stub_settings("这不是 base64")).key(1)

    def test_settings_already_refuses_a_bad_key_earlier(self):
        """**上面那两道是第二层。** 第一层在 `Settings` 的校验器里,
        而两层都在是对的:配置那层挡的是"人填错了",这一层挡的是
        "密钥源返回了意外的东西"。
        """
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            settings(master_key="这不是 base64")


class TestCryptoUsesTheProvider:
    """**`crypto.py` 只认协议,不认 `.env`。** 这一组是那件事的证据。"""

    def test_a_custom_provider_is_used_for_both_directions(self, monkeypatch):
        """换一个密钥源,加解密照走 —— 而 `crypto.py` 一行没改。
        换 KMS 那天要发生的就是这件事。"""
        key = os.urandom(32)

        class Fake:
            current_version = 7

            def key(self, version: int) -> bytes:
                assert version == 7
                return key

        monkeypatch.setattr(keys, "for_settings", lambda _s: Fake())

        blob = crypto.encrypt("授权码", user_id="u1", kind="imap", settings=settings())
        assert crypto.key_version_of(blob) == 7
        assert crypto.decrypt(blob, user_id="u1", kind="imap", settings=settings()) == "授权码"

    def test_a_provider_failure_becomes_a_decrypt_error(self, monkeypatch):
        """`KeyUnavailable` 说的是"密钥源出了问题",`DecryptError` 说的是
        "这条密文处理不了" —— 对调用方是同一件事,所以统一。
        **但原始异常挂在 `__cause__` 上**:换 KMS 之后"网络不通"和
        "密钥被删了"要能分得开。"""
        blob = crypto.encrypt("授权码", user_id="u1", kind="imap", settings=settings())

        class Broken:
            current_version = 1

            def key(self, version: int) -> bytes:
                raise KeyUnavailable("KMS 连不上")

        monkeypatch.setattr(keys, "for_settings", lambda _s: Broken())

        with pytest.raises(crypto.DecryptError) as caught:
            crypto.decrypt(blob, user_id="u1", kind="imap", settings=settings())
        assert isinstance(caught.value.__cause__, KeyUnavailable)

    def test_it_never_falls_back_to_no_encryption(self, monkeypatch):
        """**拿不到密钥一定要炸。** 退化成不加密的表现是一切正常,
        而凭据以明文躺在库里 —— 那是这个模块最不能有的失效方式。"""
        class Broken:
            current_version = 1

            def key(self, version: int) -> bytes:
                raise KeyUnavailable("拿不到")

        monkeypatch.setattr(keys, "for_settings", lambda _s: Broken())

        with pytest.raises(Exception):  # noqa: B017
            crypto.encrypt("授权码", user_id="u1", kind="imap", settings=settings())


def test_there_is_no_pretend_kms_implementation():
    """**刻意没有写一个连不上任何东西的 KmsProvider。**

    写了只会多一份没人跑过的代码,并且让人以为那条门槛已经过了。
    真正接的那天加一个类、在 `for_settings()` 里加一个分支,外加一条 ADR。
    """
    implementations = [
        name for name in dir(keys) if name.endswith("KeyProvider") and not name.startswith("_")
    ]
    assert implementations == ["EnvKeyProvider", "KeyProvider"]
