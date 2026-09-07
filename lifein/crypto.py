"""凭据字段级加密。

ADR-009:所有 IMAP 授权码、企微 token 这类凭据**存库前加密**。
"先明文存,以后再加密"行不通 —— 等你想加密时,库里已经有真实凭据,
备份里也有,而你不知道备份散在哪。所以这个模块必须在第一条凭据入库之前存在。

算法 AES-256-GCM。选它不是在选算法,是在选**不自己拼装密码学**:
GCM 自带完整性校验,密文被改会解密失败而不是给出一段垃圾明文。

信封格式(定死,改它等于改数据格式,要写迁移):

    magic(2B) | version(1B) | key_version(4B, 大端) | nonce(12B) | ciphertext+tag

`key_version` 明文放在信封里,是为了轮换:解密时先看这条密文是哪把钥匙加密的,
再决定用当前主密钥还是 MASTER_KEY_PREVIOUS。没有它就没法平滑换密钥
(docs/06-data-model.md §2.10)。

**AAD 绑定 user_id 与 kind**:一条密文被搬到另一个用户、另一个 kind 的行上会解密失败。
这样"把别人那行的密文复制到自己行上"这种攻击在密码学层面就不成立,不靠应用层记得校验。
"""

from __future__ import annotations

import base64
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from lifein.config import Settings

_MAGIC = b"LI"
_FORMAT_VERSION = 1
_NONCE_LEN = 12
_HEADER = struct.Struct(">2sBI")  # magic, format_version, key_version
_HEADER_LEN = _HEADER.size


class DecryptError(RuntimeError):
    """解密失败。密钥不对、密文被改、或信封格式不认识。"""


def _key_bytes(b64: str) -> bytes:
    raw = base64.b64decode(b64, validate=True)
    if len(raw) != 32:
        raise DecryptError("主密钥长度不是 32 字节")
    return raw


def _aad(user_id: str, kind: str) -> bytes:
    # 用 | 分隔即可:user_id 是 UUID,kind 是受控枚举,都不含 |
    return f"{user_id}|{kind}".encode()


def encrypt(plaintext: str, *, user_id: str, kind: str, settings: Settings) -> bytes:
    """加密一条凭据。返回值直接存 `credentials.ciphertext`(BYTEA)。

    永远用**当前**主密钥加密。轮换时重新加密一遍存量,不会出现新数据用旧钥匙。
    """
    key = _key_bytes(settings.master_key.get_secret_value())
    nonce = os.urandom(_NONCE_LEN)
    header = _HEADER.pack(_MAGIC, _FORMAT_VERSION, settings.master_key_version)
    blob = AESGCM(key).encrypt(nonce, plaintext.encode(), _aad(user_id, kind))
    return header + nonce + blob


def key_version_of(ciphertext: bytes) -> int:
    """读出这条密文是用哪个版本的主密钥加的,不解密。轮换扫描用。"""
    if len(ciphertext) < _HEADER_LEN + _NONCE_LEN:
        raise DecryptError("密文长度不足,不是本系统的信封")
    magic, fmt, key_version = _HEADER.unpack(ciphertext[:_HEADER_LEN])
    if magic != _MAGIC:
        raise DecryptError("信封头不匹配,这不是 LifeIn 加密的数据")
    if fmt != _FORMAT_VERSION:
        raise DecryptError(f"不认识的信封版本 {fmt}")
    return key_version


def decrypt(ciphertext: bytes, *, user_id: str, kind: str, settings: Settings) -> str:
    """解密一条凭据。

    信封里的 `key_version` 与当前主密钥版本不一致时,自动改用 MASTER_KEY_PREVIOUS。
    没配旧密钥就直接报错 —— 说明轮换做了一半,这种情况必须让人看见,不能静默失败。
    """
    key_version = key_version_of(ciphertext)
    if key_version == settings.master_key_version:
        key_b64 = settings.master_key.get_secret_value()
    elif settings.master_key_previous is not None:
        key_b64 = settings.master_key_previous.get_secret_value()
    else:
        raise DecryptError(
            f"这条密文用的是主密钥版本 {key_version},当前版本 "
            f"{settings.master_key_version},且未配置 MASTER_KEY_PREVIOUS —— "
            "轮换做了一半,见 docs/07-config.md §2.2"
        )

    nonce = ciphertext[_HEADER_LEN : _HEADER_LEN + _NONCE_LEN]
    body = ciphertext[_HEADER_LEN + _NONCE_LEN :]
    try:
        return AESGCM(_key_bytes(key_b64)).decrypt(nonce, body, _aad(user_id, kind)).decode()
    except InvalidTag as exc:
        # 不把底层异常直接抛出去:它的措辞会让人以为是数据损坏,
        # 而最常见的原因其实是 user_id/kind 对不上,或者换了密钥没重加密。
        raise DecryptError("解密失败:密钥不匹配,或密文与 user_id/kind 不对应") from exc


def needs_rotation(ciphertext: bytes, settings: Settings) -> bool:
    """这条密文是否还停在旧密钥上。轮换收尾时用它确认"无残留旧版本"。"""
    return key_version_of(ciphertext) != settings.master_key_version
