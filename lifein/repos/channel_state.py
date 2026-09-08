"""`channel_state` 的读写 —— 入站通道自己要记住的一点东西。

**这里放的都是"丢了不会出事"的状态。** 长轮询游标丢了就重新同步一次,
`context_token` 丢了就退化成不带 token 发送。所以它不进 `credentials`
(那是加密的、丢了要重配的),也不进业务表。

判断一个值该不该放这里,就问一句:**丢了会怎样?** 答案是"重来一次就好"
才放这儿;答案是"数据没了"或"要重新扫码",那它属于别的表。
"""

from __future__ import annotations

import logging

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


def get_state(user_id: str, session: Session, *, channel: str, key: str) -> str | None:
    return session.execute(_SELECT, {"user_id": user_id, "channel": channel, "key": key}).scalar()


def set_state(user_id: str, session: Session, *, channel: str, key: str, value: str) -> None:
    session.execute(_UPSERT, {"user_id": user_id, "channel": channel, "key": key, "value": value})


def clear_state(user_id: str, session: Session, *, channel: str, key: str) -> None:
    """删掉一个键。会话重建后游标必须清掉 —— 旧游标对新会话没有意义。"""
    session.execute(_DELETE, {"user_id": user_id, "channel": channel, "key": key})
