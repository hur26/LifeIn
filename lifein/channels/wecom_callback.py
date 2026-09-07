"""企业微信回调:签名校验与消息解密。

**这是本项目唯一一个对公网开放的入口**,所以它的默认姿态是拒绝:
任何一步对不上就抛异常,不做"尽力解析"。

处理顺序是有讲究的,不能换:

    1. 限长        —— 先挡掉超大 body,再谈解析
    2. 取出 Encrypt —— 用正则,**不用 XML 解析器**(见下)
    3. 校验签名     —— 常数时间比较,不能用 ==
    4. 时间戳新鲜度  —— 挡重放
    5. 解密        —— AES-256-CBC
    6. 校验 receiveid —— 密文里带着 corpid,不是自己的一律丢

**为什么不用 XML 解析器**:签名校验之前必须先从 XML 里取出 `Encrypt` 字段,
也就是说那一刻我们在解析**未经认证的**外部 XML。Python 的 ElementTree
对实体扩展攻击(billion laughs)没有防护,引 defusedxml 又要先写 ADR。
而这里的载荷结构是固定的、只需要一个字段 —— 用正则取,XXE 和实体扩展
这一整类问题就不存在了。解密之后的内层 XML 同理。

**签名用 SHA1 是企微定的**,不是我们选的。它防的是伪造而不是碰撞,
真正的机密性来自 AES 那一层。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import struct
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
"""body 上限。企微的消息体远小于这个数;超了说明不是它发的。"""

DEFAULT_MAX_SKEW_S = 300
"""时间戳容差。超出即拒收,挡的是重放(R2 里"重复执行"那半)。"""

_BLOCK_SIZE = 32
_ENCRYPT_TAG = re.compile(r"<Encrypt>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</Encrypt>", re.DOTALL)


def _tag(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"<{name}>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</{name}>",
        re.DOTALL,
    )


_FIELDS = {
    name: _tag(name) for name in ("FromUserName", "MsgType", "Content", "MsgId", "CreateTime")
}


class CallbackRejected(Exception):
    """回调不可信。**不要在响应里回显原因** —— 那等于给探测者做提示。"""


@dataclass(frozen=True)
class InboundMessage:
    from_user: str
    msg_type: str
    content: str
    msg_id: str
    created_at: datetime


def compute_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """企微规定的算法:四个值排序后拼接取 SHA1。"""
    joined = "".join(sorted([token, timestamp, nonce, encrypt]))
    return hashlib.sha1(joined.encode()).hexdigest()  # noqa: S324 —— 协议规定,非我们选择


class WecomCallback:
    def __init__(
        self,
        *,
        token: str,
        aes_key: str,
        corp_id: str,
        max_skew_s: int = DEFAULT_MAX_SKEW_S,
    ) -> None:
        self._token = token
        self._corp_id = corp_id
        self._max_skew_s = max_skew_s
        # 企微给的是 43 个字符的 base64,末尾少一个 '='
        self._aes_key = base64.b64decode(aes_key + "=")
        if len(self._aes_key) != 32:
            raise ValueError("WECOM_CALLBACK_AES_KEY 解码后应为 32 字节")

    # ---------- URL 验证(GET)----------

    def verify_url(self, *, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """企微配置回调地址时的一次性握手。返回要原样回显的明文。"""
        self._check_signature(msg_signature, timestamp, nonce, echostr)
        # 这一步不校验时间戳:企微后台点"保存"时才发,它本来就是人工触发的
        return self._decrypt(echostr)

    # ---------- 消息(POST)----------

    def parse_message(
        self,
        *,
        body: bytes,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        now: float | None = None,
    ) -> InboundMessage:
        if len(body) > MAX_BODY_BYTES:
            raise CallbackRejected(f"body 超过 {MAX_BODY_BYTES} 字节")

        match = _ENCRYPT_TAG.search(body.decode("utf-8", errors="replace"))
        if not match:
            raise CallbackRejected("body 里没有 Encrypt 字段")
        encrypt = match.group(1).strip()

        self._check_signature(msg_signature, timestamp, nonce, encrypt)
        self._check_freshness(timestamp, now)

        plain = self._decrypt(encrypt)
        return self._parse_inner(plain)

    # ---------- 内部 ----------

    def _check_signature(self, given: str, timestamp: str, nonce: str, encrypt: str) -> None:
        expected = compute_signature(self._token, timestamp, nonce, encrypt)
        # 常数时间比较:用 == 会因为比较提前返回而泄露前缀是否命中
        if not hmac.compare_digest(expected, given or ""):
            raise CallbackRejected("签名不匹配")

    def _check_freshness(self, timestamp: str, now: float | None) -> None:
        try:
            sent_at = int(timestamp)
        except (TypeError, ValueError) as exc:
            raise CallbackRejected("timestamp 不是整数") from exc
        current = time.time() if now is None else now
        if abs(current - sent_at) > self._max_skew_s:
            raise CallbackRejected("timestamp 超出容差,可能是重放")

    def _decrypt(self, encrypt: str) -> str:
        try:
            ciphertext = base64.b64decode(encrypt)
        except Exception as exc:  # noqa: BLE001
            raise CallbackRejected("Encrypt 不是合法 base64") from exc
        if not ciphertext or len(ciphertext) % 16 != 0:
            raise CallbackRejected("密文长度不合法")

        # IV 取密钥前 16 字节,是企微协议规定的
        decryptor = Cipher(algorithms.AES(self._aes_key), modes.CBC(self._aes_key[:16])).decryptor()
        plain = decryptor.update(ciphertext) + decryptor.finalize()

        pad = plain[-1] if plain else 0
        if not 1 <= pad <= _BLOCK_SIZE:
            raise CallbackRejected("填充不合法")
        plain = plain[:-pad]

        if len(plain) < 20:
            raise CallbackRejected("解密后长度不足")
        msg_len = struct.unpack(">I", plain[16:20])[0]
        if msg_len > len(plain) - 20:
            raise CallbackRejected("声明的消息长度超出实际内容")

        message = plain[20 : 20 + msg_len]
        receive_id = plain[20 + msg_len :].decode("utf-8", errors="replace")

        # 密文里带着 corpid。别人拿走我们的回调地址也伪造不出这一段
        if not hmac.compare_digest(receive_id, self._corp_id):
            raise CallbackRejected("receiveid 与本企业不符")

        return message.decode("utf-8", errors="replace")

    @staticmethod
    def _parse_inner(xml: str) -> InboundMessage:
        values = {}
        for name, pattern in _FIELDS.items():
            found = pattern.search(xml)
            values[name] = found.group(1).strip() if found else ""

        try:
            created = datetime.fromtimestamp(int(values["CreateTime"] or 0), tz=UTC)
        except (ValueError, OSError):
            created = datetime.now(UTC)

        if not values["FromUserName"]:
            raise CallbackRejected("消息里没有 FromUserName")

        return InboundMessage(
            from_user=values["FromUserName"],
            msg_type=values["MsgType"] or "unknown",
            content=values["Content"],
            msg_id=values["MsgId"],
            created_at=created,
        )
