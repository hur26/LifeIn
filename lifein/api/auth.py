"""请求签名与短期 token —— [06 §6.2 / §6.3](../../docs/06-data-model.md#6-接口契约)。

**这个模块不依赖 FastAPI,也不碰数据库。** 它只做算术:算签名串、比签名、
签 token、解 token。谁去库里取密钥、取不到怎么办,是 `deps.py` 的事。
分开的理由很实在 —— 密码学部分的错误没有外部表现(签名照样能算出来,
只是不对),必须能脱开 HTTP 单独测。

两处刻意的选择:

**比较用 `compare_digest`。** 逐字节短路比较会随比对位置泄露耗时,
本地网络里这个信道很窄但不是零,而这里没有任何理由省那几纳秒。

**token 用设备密钥签,不用全局密钥。** R11 要求"服务端可单点吊销",
而自洽的 token 在过期前谁也拦不住。把签名密钥绑在设备凭据上,
`revoked_at` 一填,那台设备手上的 token 下一次请求就失效 —— 不需要第二张表。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime

TOKEN_VERSION = "v1"

HEADER_USER = "X-LifeIn-User"
HEADER_DEVICE = "X-LifeIn-Device"
HEADER_TIMESTAMP = "X-LifeIn-Timestamp"
HEADER_SIGNATURE = "X-LifeIn-Signature"


class AuthError(Exception):
    """签名或 token 不成立。

    **原因只给日志看。** 返给对方的永远是一个不带任何信息的 401 ——
    告诉探测者他错在哪一步,等于帮他调试(和企微回调那条同源)。
    """


@dataclass(frozen=True)
class TokenPayload:
    user_id: str
    device_id: str
    expires_at: datetime

    def is_expired(self, *, now: datetime) -> bool:
        return now >= self.expires_at


def signing_string(*, method: str, path: str, timestamp: str, body: bytes) -> bytes:
    """要被签的那串东西。

    四样都进签名:方法、路径、时间戳、**请求体的摘要**。少任何一样,
    截获的请求就能被改成另一个意思重放 —— 少了 path,一次心跳能被改成一次上报;
    少了 body 摘要,上报的内容能被整个换掉。

    `path` 是反向代理**转发之后**的路径,所以代理不能改写路径
    (07 §5 的部署步骤里那条 "反代 + TLS" 默认是透传的)。
    """
    digest = hashlib.sha256(body).hexdigest()
    return "\n".join([method.upper(), path, timestamp, digest]).encode()


def sign(secret: str, message: bytes) -> str:
    """用设备密钥算 HMAC-SHA256,十六进制。

    密钥是 base64 存的,签名前解回字节 —— 拿 base64 字符串本身当密钥也能跑,
    但那样有效熵少四分之一,而这里没有理由白扔。
    """
    return hmac.new(_key(secret), message, hashlib.sha256).hexdigest()


def verify(secret: str, message: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign(secret, message), signature.strip().lower())


def timestamp_is_fresh(raw: str, *, now: datetime, max_skew_s: int) -> bool:
    """时间戳在允许的偏移内。这是重放保护的第一条(第二条是业务幂等)。

    **两个方向都要卡。** 只卡"太旧"的话,一个把时钟调到明年的设备
    能签出永远新鲜的请求,时间窗就等于没有。
    """
    try:
        sent = int(raw)
    except (TypeError, ValueError):
        return False
    return abs(int(now.timestamp()) - sent) <= max_skew_s


def mint_token(*, secret: str, user_id: str, device_id: str, expires_at: datetime) -> str:
    """签一个短期 token:`v1.<base64url(payload)>.<hex 签名>`。

    payload 是明文的(base64url 不是加密),这是有意的 —— 服务端要先看出
    它是哪台设备的,才知道该拿哪把密钥去验签。里面没有秘密:
    user_id 和 device_id 本来就在每个请求的头里。
    """
    head = f"{TOKEN_VERSION}.{_encode({'u': user_id, 'd': device_id, 'exp': _epoch(expires_at)})}"
    return f"{head}.{sign(secret, head.encode())}"


def parse_token(token: str) -> TokenPayload:
    """先解出这是谁的 token(**还没验签**),好去库里取那台设备的密钥。

    解出来的东西在验签之前一个字都不能信,所以这里只解不判断 ——
    调用方拿它去取密钥,取不到或验不过一律是同一个 401。
    """
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != TOKEN_VERSION:
        raise AuthError("token 形状不对")

    try:
        data = json.loads(_decode(parts[1]))
        return TokenPayload(
            user_id=str(data["u"]),
            device_id=str(data["d"]),
            expires_at=datetime.fromtimestamp(int(data["exp"]), tz=UTC),
        )
    except Exception as exc:  # noqa: BLE001 —— 解坏了就是解坏了,不区分坏在哪
        raise AuthError(f"token 解不开:{type(exc).__name__}") from exc


def verify_token(token: str, *, secret: str, now: datetime) -> TokenPayload:
    """验签 + 看有没有过期。两样都过才返回 payload。"""
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise AuthError("token 形状不对")

    payload = parse_token(token)
    head = f"{parts[0]}.{parts[1]}"
    if not verify(secret, head.encode(), parts[2]):
        raise AuthError("token 签名不对")
    if payload.is_expired(now=now):
        raise AuthError("token 已过期")
    return payload


def _key(secret: str) -> bytes:
    try:
        return base64.b64decode(secret, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise AuthError("设备密钥不是合法的 base64") from exc


def _encode(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode(chunk: str) -> bytes:
    # base64url 去掉了填充,补回来再解 —— 少了这一步,长度不是 4 的倍数的
    # payload 会解失败,而那取决于 user_id 的长度,平时测不出来
    padding = "=" * (-len(chunk) % 4)
    return base64.urlsafe_b64decode(chunk + padding)


def _epoch(moment: datetime) -> int:
    if moment.tzinfo is None:
        raise AuthError("过期时间必须带时区")
    return int(moment.timestamp())
