"""一次性换取码(06 §6.15,P4 第 1 片)。

**它补的是今天就存在的一个洞。** `admin issue-device` 打出来的二维码里是
明文的两把密钥 —— 自己扫没问题,而 03 的 P4 说朋友要用它配码,
那张图会走微信发过去,**等于把密钥发在聊天里**。

## 存哈希,不存码

和存密码一样:库被拖走时,里面的东西不该直接可用。码只在生成那一刻显示一次,
之后服务端自己也说不出它是什么。

## 一次性 + 短命,两条都要

只一次性不短命:码在聊天记录里躺三天,期间任何看到那张图的人都能抢先换走 ——
而你要到自己换不了的时候才发现。

只短命不一次性:十分钟内被别人多换一次,你和他各有一套密钥,
而**两套都是有效的**,你不会发现任何异常。

## 换过的码不删行

`claimed_at` 而不是 `DELETE`。"这台设备是什么时候、用哪个码配上的"是排查
"我的号被别人配走了吗"唯一的线索,而删掉那一行之后这个问题就没法回答了。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

NO_USER_ID_REQUIRED = frozenset({"claim", "peek", "purge_expired"})
"""这三个不收 `user_id`,而**这是唯一说得通的一次例外**(铁律 1 的守则见
`tests/test_repo_contract.py`)。

`claim()` 的调用方**手上还没有 user_id** —— 它正是要算出这个值:配码那一刻
App 只有一张码,而"这张码是谁的"就存在这一行里。要求它先给 user_id,
等于要求 App 先知道自己配的是谁的号,而那正是这张码要告诉它的事。
(`users.find_by_wecom_userid` 破例的理由一模一样。)

`peek()` 是 `claim()` 的只读版,`purge_expired()` 按时间扫全表 ——
两者跟着一起破例,而它们**都不返回任何属于某个用户的数据**:
`peek` 只回码本身的状态,`purge` 只回删了几行。
"""

DEFAULT_TTL = timedelta(minutes=10)
"""码活多久。**配码是一个当面或即时的动作** —— 十分钟够走完,
而在聊天记录里躺三天的码等于没有一次性。"""

INVITE_VERSION = 2
"""二维码里那个 payload 的版本号。

**放在这里而不是放在出码的那一处**:出码的地方有两个(Web 控制台和
`admin invite`),而 App 认的只有一个数(`EnrollmentPayload.INVITE_VERSION`)。
各写各的话,先对不上的是没人核对的那一处 —— 而症状是新出的码被 App 当成
缺字段的旧式配码,报一句完全指错方向的话。

`v:1` 是 `issue-device` 那种(payload 里直接是两把密钥),它没有版本号字段,
所以 App 那边是"不等于 2 就按旧的解"。
"""

CODE_BYTES = 16
"""128 位随机。**防爆破靠这个长度,不靠限流** ——
限流要存计数状态,那又是一张表,而穷举 2^128 不现实。"""


@dataclass(frozen=True)
class EnrollmentCode:
    id: int
    user_id: str
    purpose: str
    base_url: str
    expires_at: datetime
    claimed_at: datetime | None = None
    claimed_by: str | None = None

    def is_usable(self, *, now: datetime) -> bool:
        return self.claimed_at is None and self.expires_at > now


_COLUMNS = "id, user_id, purpose, base_url, expires_at, claimed_at, claimed_by"

_INSERT = text(f"""
    INSERT INTO enrollment_codes (user_id, code_hash, purpose, base_url, expires_at)
    VALUES (:user_id, :code_hash, :purpose, :base_url, :expires_at)
    RETURNING {_COLUMNS}
""")

_SELECT_BY_HASH = text(f"SELECT {_COLUMNS} FROM enrollment_codes WHERE code_hash = :code_hash")

_CLAIM = text(f"""
    UPDATE enrollment_codes
       SET claimed_at = :now, claimed_by = :device_id
     WHERE code_hash = :code_hash
       -- **判断和写入是同一条语句。** 两个人同时扫同一张图时,
       -- 只有一个能换走 —— 先查再写的话两个都能换,而那时你和他各有一套
       -- 有效的密钥,你不会发现任何异常
       AND claimed_at IS NULL
       AND expires_at > :now
 RETURNING {_COLUMNS}
""")

_LIST = text(f"""
    SELECT {_COLUMNS} FROM enrollment_codes
     WHERE user_id = :user_id
     ORDER BY created_at DESC
     LIMIT :limit
""")

_PURGE = text("DELETE FROM enrollment_codes WHERE expires_at <= :cutoff AND claimed_at IS NULL")


def issue(
    user_id: str,
    session: Session,
    *,
    base_url: str,
    now: datetime,
    purpose: str = "all",
    ttl: timedelta = DEFAULT_TTL,
) -> tuple[str, EnrollmentCode]:
    """生成一张码。**返回的明文只有这一次能拿到。**

    库里存的是它的 sha256,所以服务端自己也说不出这个码是什么 ——
    和存密码同一个道理:库被拖走时,里面的东西不该直接可用。
    """
    code = secrets.token_urlsafe(CODE_BYTES)
    row = session.execute(
        _INSERT,
        {
            "user_id": user_id,
            "code_hash": _hash(code),
            "purpose": purpose,
            "base_url": base_url,
            "expires_at": now + ttl,
        },
    ).one()
    return code, _to_code(row)


def claim(session: Session, *, code: str, device_id: str, now: datetime) -> EnrollmentCode | None:
    """把码换掉。**换不了就返回 None —— 三种原因不区分。**

    码不对、用过了、过期了,对调用方来说都是"这个码不能用"。区分等于告诉
    对方"这个码存在过",而那是爆破时唯一有用的信息(06 §6.15)。

    **失败要记进日志。** 连续的失败是"有人在扫这个接口"唯一的信号 ——
    这个接口不带认证(它换的就是认证),所以它是这个系统里唯一需要盯的入口。
    """
    row = session.execute(
        _CLAIM, {"code_hash": _hash(code), "device_id": device_id, "now": now}
    ).first()
    if row is None:
        log.warning("配码换取失败:device_id=%s(码不对、用过了、或者过期了)", device_id)
        return None
    claimed = _to_code(row)
    log.info("配码换取成功:user=%s device_id=%s", claimed.user_id, device_id)
    return claimed


def peek(session: Session, *, code: str) -> EnrollmentCode | None:
    """只看不换。**给 `admin` 排查用,不给接口用** ——
    接口那边一律走 `claim()`,因为"先看再换"中间那一瞬就是两个人都能换的空间。
    """
    row = session.execute(_SELECT_BY_HASH, {"code_hash": _hash(code)}).first()
    return _to_code(row) if row else None


def list_codes(user_id: str, session: Session, *, limit: int = 20) -> list[EnrollmentCode]:
    rows = session.execute(_LIST, {"user_id": user_id, "limit": limit}).all()
    return [_to_code(row) for row in rows]


def purge_expired(session: Session, *, cutoff: datetime) -> int:
    """删掉过期且没被用过的。**用过的不删** —— 那是"谁什么时候配上的"的痕迹。"""
    return session.execute(_PURGE, {"cutoff": cutoff}).rowcount


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _to_code(row) -> EnrollmentCode:
    return EnrollmentCode(
        id=row.id,
        user_id=str(row.user_id),
        purpose=row.purpose,
        base_url=row.base_url,
        expires_at=row.expires_at,
        claimed_at=row.claimed_at,
        claimed_by=row.claimed_by,
    )
