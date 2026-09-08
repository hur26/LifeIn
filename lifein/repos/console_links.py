"""控制台的一次性链接(P4 第 9 片)。

**浏览器打开一个链接时带不了 `Authorization` 头** —— 所以 App 里那套 Bearer
token 在 Web 上直接用不了。这个模块是那个问题的答案:App 里点一下,
换一个短命的 token 放进 URL,跳过去。

## 和配码那张码是同一个形状

存哈希不存明文、短命、按内容判断能不能用 —— 和
[`repos/enrollment.py`](enrollment.py) 一模一样。**同一个形状用两次,
比为第二次发明一套新的更可靠**:看代码的人不用学两套,
而两套里总有一套会先出错。

## 但它不是一次性的

配码那张码换过就作废,因为它换的是长期密钥。这个链接不同:
**页面上有链接要点**(导出、隐私说明),而每点一次就换一张 token
意味着每次点击都要回 App 一趟。

所以它是**短命的多次可用** —— 十五分钟内这一张一直有效,过期就回首页。
安全性靠的是那十五分钟,不是"只能用一次"。

## URL 里带 token 的代价,以及为什么仍然接受

token 会进浏览器历史,可能进 Referer(所以页面里没有任何外链)。
十五分钟之后它什么都不是,而**替代方案是给这个系统再加一个登录面**,
那比一条会过期的历史记录危险得多(R11 已经有够多的凭据面了)。
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

NO_USER_ID_REQUIRED = frozenset({"resolve", "purge_expired"})
"""`resolve()` 的调用方手上还没有 `user_id` —— 它正是要算出这个值,
和 `enrollment.claim` 破例的理由一模一样(见 `tests/test_repo_contract.py`)。
`purge_expired` 按时间扫全表,不返回任何属于某个用户的数据。"""

DEFAULT_TTL = timedelta(minutes=15)
TOKEN_BYTES = 16


@dataclass(frozen=True)
class ConsoleLink:
    id: int
    user_id: str
    expires_at: datetime


_INSERT = text("""
    INSERT INTO console_links (user_id, token_hash, expires_at)
    VALUES (:user_id, :token_hash, :expires_at)
    RETURNING id, user_id, expires_at
""")

_RESOLVE = text("""
    SELECT user_id FROM console_links
     WHERE token_hash = :token_hash AND expires_at > :now
""")

_PURGE = text("DELETE FROM console_links WHERE expires_at <= :cutoff")


def issue(
    user_id: str, session: Session, *, now: datetime, ttl: timedelta = DEFAULT_TTL
) -> tuple[str, ConsoleLink]:
    """发一张。**明文只有这一次能拿到**,库里存的是它的 sha256。"""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    row = session.execute(
        _INSERT,
        {"user_id": user_id, "token_hash": _hash(token), "expires_at": now + ttl},
    ).one()
    return token, ConsoleLink(id=row.id, user_id=str(row.user_id), expires_at=row.expires_at)


def resolve(session: Session, *, token: str, now: datetime) -> str | None:
    """这张 token 是谁的。**过期或不认识都返回 None,不区分。**"""
    row = session.execute(_RESOLVE, {"token_hash": _hash(token), "now": now}).first()
    if row is None:
        log.info("控制台链接无效或已过期")
        return None
    return str(row.user_id)


def purge_expired(session: Session, *, cutoff: datetime) -> int:
    """删掉过期的。**这里可以真删** —— 和配码那张不一样:
    配码要留"谁什么时候配上的"的痕迹,而"某人打开过控制台"不是那种事。
    """
    return session.execute(_PURGE, {"cutoff": cutoff}).rowcount


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
