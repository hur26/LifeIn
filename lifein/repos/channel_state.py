"""`channel_state` 的读写 —— 入站通道自己要记住的一点东西。

**这里放的都是"丢了不会出事"的状态。** 长轮询游标丢了就重新同步一次,
`context_token` 丢了就退化成不带 token 发送。所以它不进 `credentials`
(那是加密的、丢了要重配的),也不进业务表。

判断一个值该不该放这里,就问一句:**丢了会怎样?** 答案是"重来一次就好"
才放这儿;答案是"数据没了"或"要重新扫码",那它属于别的表。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

_UPSERT = text("""
    INSERT INTO channel_state (user_id, channel, key, value)
    VALUES (:user_id, :channel, :key, :value)
    ON CONFLICT (user_id, channel, key)
    DO UPDATE SET value = EXCLUDED.value, updated_at = now()
""")

_SELECT = text("""
    SELECT value FROM channel_state
     WHERE user_id = :user_id AND channel = :channel AND key = :key
""")

_DELETE = text("""
    DELETE FROM channel_state
     WHERE user_id = :user_id AND channel = :channel AND key = :key
""")

_CLAIM_LEASE = text("""
    INSERT INTO channel_state (user_id, channel, key, value)
    VALUES (:user_id, :channel, :key, :owner)
    ON CONFLICT (user_id, channel, key) DO UPDATE
       SET value = EXCLUDED.value, updated_at = now()
     WHERE channel_state.value = :owner
        OR channel_state.updated_at < now() - CAST(:ttl AS INTERVAL)
    RETURNING value
""")
"""抢一条租约。**判断和写入在同一条语句里。**

先查后写会在并发下让两个进程同时"抢到" —— 而这条租约存在的全部意义
就是不让那件事发生。形状和 `job_runs.claim_window` 一样,
那边的注释里写了同一个理由。

`updated_at` 兼作续约时间戳:那一列本来就在,`ON CONFLICT DO UPDATE`
每次都会把它推到 `now()`。多一列存过期时间等于多一处会和事实对不上的地方。

**过期的比较也在 SQL 里做,用同一个 `now()`。** 写入用库的钟、比较用调用方
的钟的话,一个钟快了几分钟的进程会认为一条**活着的**租约已经过期,
然后合法地抢走它 —— 于是两个长轮询一起跑,而这条租约存在的全部意义
就是不让那件事发生。两边同一个钟,时钟偏移就不再是一个变量。
"""


def get_state(user_id: str, session: Session, *, channel: str, key: str) -> str | None:
    return session.execute(_SELECT, {"user_id": user_id, "channel": channel, "key": key}).scalar()


def set_state(user_id: str, session: Session, *, channel: str, key: str, value: str) -> None:
    session.execute(_UPSERT, {"user_id": user_id, "channel": channel, "key": key, "value": value})


def claim_lease(
    user_id: str,
    session: Session,
    *,
    channel: str,
    key: str,
    owner: str,
    ttl: timedelta,
) -> bool:
    """抢或者续一条租约。**抢不到就返回 False。**

    干什么用的:**一个 iLink token 同时只能有一个长轮询**。两个客户端一起拉
    会互相抢消息 —— 表现是"消息一会儿到一会儿不到",而两边的日志各自
    看起来都正常。这在一台机器上跑着开发进程和正式服务时会真发生。

    续约就是再抢一次:`value = :owner` 那一支让自己人永远抢得到,
    而别人只有在租约过期(`updated_at < stale_before`)之后才抢得走。

    **多久算死由调用方给**,因为那是那条循环的性质,不是这张表的。
    但**"现在几点"由数据库说了算** —— 见 `_CLAIM_LEASE` 的说明。
    """
    row = session.execute(
        _CLAIM_LEASE,
        {
            "user_id": user_id,
            "channel": channel,
            "key": key,
            "owner": owner,
            # INTERVAL 要一个字符串。秒是最不容易读错的单位
            "ttl": f"{ttl.total_seconds()} seconds",
        },
    ).first()
    if row is None:
        log.info("租约 %s/%s 抢不到:另一个进程正拿着它", channel, key)
        return False
    return True


def clear_state(user_id: str, session: Session, *, channel: str, key: str) -> None:
    """删掉一个键。会话重建后游标必须清掉 —— 旧游标对新会话没有意义。"""
    session.execute(_DELETE, {"user_id": user_id, "channel": channel, "key": key})
