"""运营层控制台的口令与会话([ADR-029](../docs/04-tech-decisions.md))。

控制台分两层:**用户层不登录**(入口是 App 里点出来的一次性链接),
**运营层要口令**。这个模块是后者的全部密码学部分 —— 它被 API 和
`lifein.admin` 两边用,所以不放在 `api/` 下面。

## 口令不进库

存的是 `scrypt` 派生值,放在 `CONSOLE_ADMIN_PASSWORD_HASH` 里,
和 `MASTER_KEY` 同一处(07 §2.8)。**这一份口令的权限范围是全部人的
全部数据,它不该和它保护的东西躺在同一个地方** —— 和"导出文件里不放凭据"
是同一条判断。

选 `scrypt` 不是在选算法,是在**不引第三个依赖**:它在标准库里
(`hashlib.scrypt`),而 bcrypt / argon2 都要装包,装包要先写 ADR
(AGENTS.md §3)。参数按"一次校验大约几十毫秒"取 —— 登录一天几次,
慢一点没人感觉得到,而离线爆破每一次都要付同样的代价。

## 会话是签出来的,不是存出来的

`lifein_admin` cookie 里是一串自带签名的东西,**库里没有对应的行**。
不建表的理由不是省事,是"过期就是过期":一张存在库里的会话表要有人清理,
而没被清理的会话表是一份**看起来已经登出、实际还能用**的清单。

签名密钥从主密钥派生(HMAC 一次),不是直接拿主密钥来签 ——
**一把钥匙只做一件事**:主密钥漏了这个会话签名也就没意义了,
反过来则不成立,而反过来正是更可能发生的那个方向。

## 失败闸门在进程里,不在库里

它拦的是"拿着一本字典对着登录页跑",而那种事发生在**分钟级**,
重启一次进程就重置对它没有帮助 —— 重启是运维动作,不是攻击者能触发的动作。

**真正的防线是 scrypt 那几十毫秒**,闸门只是让那几十毫秒不必被付上一万次。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from lifein import keys
from lifein.config import Settings

log = logging.getLogger(__name__)

SCHEME = "scrypt"
"""编码里的第一段。**留着它是为了将来换得动** —— 换算法那天旧的那些
还认得出自己是旧的,而不是变成一串谁也不知道怎么校验的东西。"""

_N = 1 << 15
_R = 8
_P = 1
_DK_LEN = 32
_SALT_BYTES = 16
_MAXMEM = 128 * _N * _R * 2
"""`hashlib.scrypt` 的默认 `maxmem` 挡在 32 MB,而 N=32768、r=8 正好要 32 MB ——
不显式给这个值的话它会直接抛 `ValueError`,而且是在**第一次有人登录的时候**。"""

_SESSION_VERSION = "v1"
_NONCE_BYTES = 12


class PasswordFormatError(ValueError):
    """`CONSOLE_ADMIN_PASSWORD_HASH` 那一格里的东西不是这个模块认得的形状。"""


def hash_password(password: str) -> str:
    """把口令变成能写进 `.env` 的一行。**口令本身哪儿都不存。**

    格式:``scrypt$<n>$<r>$<p>$<base64 盐>$<base64 派生值>``。
    参数写在编码里而不是写死在代码里 —— 调大参数那天,**已经配好的那些
    还要能校验通过**,否则一次安全加固会变成一次谁也登不进去。
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = _derive(password, salt=salt, n=_N, r=_R, p=_P)
    return "$".join(
        [SCHEME, str(_N), str(_R), str(_P), _b64(salt), _b64(derived)]
    )


def verify_password(password: str, encoded: str) -> bool:
    """口令对不对。格式不认识时返回 `False` 而不是抛。

    **一个配错了的哈希和一个错的口令,对着登录页的人看到的必须是同一句话** ——
    "格式不对"那句会告诉他这台机器上运营层是配过的,而"口令不对"什么都不告诉他。
    真正的原因进日志。
    """
    try:
        scheme, n, r, p, salt_b64, expected_b64 = encoded.split("$")
        if scheme != SCHEME:
            raise PasswordFormatError(f"不认识的算法:{scheme}")
        salt = _unb64(salt_b64)
        expected = _unb64(expected_b64)
        derived = _derive(password, salt=salt, n=int(n), r=int(r), p=int(p))
    except (ValueError, PasswordFormatError) as exc:
        log.error("CONSOLE_ADMIN_PASSWORD_HASH 配得不对:%s", exc)
        return False
    return hmac.compare_digest(derived, expected)


def issue_session(*, settings: Settings, now: datetime, ttl: timedelta) -> str:
    """签一张会话。返回的东西直接进 cookie。

    里面只有过期时间和一串随机数 —— **没有 `user_id`**:
    运营者是谁由 `users.is_admin` 那一行回答,而把它写进 cookie 意味着
    改那一行之后旧 cookie 还指着旧的人。
    """
    expires = int((now + ttl).timestamp())
    nonce = _b64(secrets.token_bytes(_NONCE_BYTES))
    body = f"{_SESSION_VERSION}.{expires}.{nonce}"
    return f"{body}.{_b64(_sign(body, settings=settings))}"


def verify_session(value: str | None, *, settings: Settings, now: datetime) -> bool:
    """这张会话还作数吗。**先验签再看时间。**

    反过来的话,一张伪造的、过期时间写在两年后的 cookie 会先通过时间检查 ——
    虽然它最后还是会被签名挡住,但那个顺序把"签名是唯一的防线"这件事
    藏了起来,而藏起来的防线是会在下一次重构里被绕过去的那种。
    """
    if not value:
        return False
    try:
        version, expires_raw, nonce, signature = value.split(".")
    except ValueError:
        return False
    if version != _SESSION_VERSION:
        return False

    body = f"{version}.{expires_raw}.{nonce}"
    try:
        given = _unb64(signature)
    except ValueError:
        return False
    if not hmac.compare_digest(_sign(body, settings=settings), given):
        return False

    try:
        expires = int(expires_raw)
    except ValueError:
        return False
    return now.timestamp() < expires


@dataclass
class LoginGate:
    """连续失败之后闸门落下。**一个进程一个,不进库**(见模块开头)。

    只数一个计数,不按 IP 分:运营者只有一个,而按 IP 分意味着换一个 IP
    就重新拿到满额的尝试次数 —— 那正是想爆破的人做得到的事。

    代价是**一个人在敲错口令时可以把自己锁在门外**,而这个代价是接受的:
    十五分钟之后它自己开,而真爆破那次不该有十五分钟里的第六次机会。
    """

    max_attempts: int
    lockout: timedelta
    failures: int = 0
    locked_until: datetime | None = None

    def blocked(self, now: datetime) -> timedelta | None:
        """还要等多久。没锁着就返回 None。"""
        if self.locked_until is None:
            return None
        if now >= self.locked_until:
            # 锁到期,连计数一起清 —— 留着计数的话下一次失败会立刻再锁上
            self.locked_until = None
            self.failures = 0
            return None
        return self.locked_until - now

    def record_failure(self, now: datetime) -> None:
        self.failures += 1
        if self.failures >= self.max_attempts:
            self.locked_until = now + self.lockout
            log.warning("运营台连续失败 %s 次,闸门落下到 %s", self.failures, self.locked_until)

    def record_success(self) -> None:
        self.failures = 0
        self.locked_until = None


def _derive(password: str, *, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=_DK_LEN,
        maxmem=max(_MAXMEM, 128 * n * r * 2),
    )


def _sign(body: str, *, settings: Settings) -> bytes:
    return hmac.new(_session_key(settings), body.encode("utf-8"), hashlib.sha256).digest()


def _session_key(settings: Settings) -> bytes:
    """会话签名密钥:主密钥派生一次,不直接用主密钥。

    走 `keys.for_settings()` 而不是读 `settings.master_key` —— 换 KMS 那天
    这里跟着一起换,不必被想起来(`lifein/keys.py` 开头那段)。
    """
    provider = keys.for_settings(settings)
    master = provider.key(provider.current_version)
    return hmac.new(master, b"lifein-console-admin-session-v1", hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
