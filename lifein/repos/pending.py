"""`pending_confirmations` 的读写 —— **一套机制服务所有 agent**(06 §2.7)。

日程 agent 和记账 agent 不许各写一套。它是 agent 五项契约里"失败行为"的
唯一落地形式:拿不准的时候不猜、不动作,进这个队列等人点(铁律 7)。

这一层有三件事是刻意设计成"做不到别的"的:

**确认与写入必须同一个事务,所以 `confirm` 收一个写入函数。**
06 §2.7 那条硬要求写着"否则会出现确认了但没写进去、或者写了两次"。
把写入交给调用方在别处做,那条要求就只能靠自觉;收进来之后,
**同一个事务是签名上的既成事实**,想分开都分不开。

**认领是一次原子 UPDATE。** `UPDATE ... WHERE status = 'pending' RETURNING`
天然排他:两个入口同时点确认(App 一次、消息里一次),第二个拿不到行,
得到的是"已经处理过了",而不是写入两遍。

**`rejected` 与 `expired` 永不删除。** 用户拒绝过什么,正是这个 agent 最该
学会不做的事 —— 那是评测集的负样本来源。删掉等于每次都从头再错一遍。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

DEFAULT_TTL = timedelta(days=30)
"""默认多久过期。06 §2.7 写的就是 30 天。

过期不是清理垃圾,是**承认这条已经没有意义了** —— 一条三十天前的"周三开会"
现在确认了反而更糟。所以它和 rejected 一样保留记录,只是不再能被写入。
"""


class PendingKind(StrEnum):
    TRANSACTION = "transaction"
    CALENDAR_EVENT = "calendar_event"
    TASK = "task"
    FACT = "fact"


class PendingStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    EDITED = "edited"
    """用户改过内容之后确认。写入的是 `resolved_payload`,不是原来的 payload。"""

    REJECTED = "rejected"
    EXPIRED = "expired"


class PendingReason(StrEnum):
    LOW_CONFIDENCE = "low_confidence"
    CHECK_FAILED = "check_failed"
    RULE_MISS = "rule_miss"
    AMBIGUOUS = "ambiguous"


class PendingError(ValueError):
    """队列本身的用法错了。都在动数据之前抛。"""


@dataclass(frozen=True)
class Pending:
    id: int
    agent: str
    kind: PendingKind
    target_table: str
    payload: dict[str, Any]
    reason: PendingReason
    confidence: float | None
    source_event_id: int | None
    status: PendingStatus
    expires_at: datetime


_COLUMNS = """
    id, agent, kind, target_table, payload, reason, confidence,
    source_event_id, status, expires_at
"""

_INSERT = text(f"""
    INSERT INTO pending_confirmations
        (user_id, agent, kind, target_table, payload, reason, confidence,
         source_event_id, expires_at)
    VALUES
        (:user_id, :agent, :kind, :target_table, CAST(:payload AS JSONB), :reason,
         :confidence, :source_event_id, :expires_at)
    RETURNING {_COLUMNS}
""")

_SELECT_ONE = text(f"""
    SELECT {_COLUMNS} FROM pending_confirmations
     WHERE user_id = :user_id AND id = :pending_id
""")

_LIST = text(f"""
    SELECT {_COLUMNS} FROM pending_confirmations
     WHERE user_id = :user_id AND status = 'pending' AND expires_at > :now
     ORDER BY created_at
     LIMIT :limit
""")

_CLAIM = text(f"""
    UPDATE pending_confirmations
       SET status = :status,
           resolved_at = now(),
           resolved_via = :via,
           resolved_payload = CAST(:resolved_payload AS JSONB)
     WHERE user_id = :user_id
       AND id = :pending_id
       AND status = 'pending'
       AND expires_at > now()
 RETURNING {_COLUMNS}
""")

_EXPIRE = text("""
    UPDATE pending_confirmations
       SET status = 'expired', resolved_at = now()
     WHERE user_id = :user_id AND status = 'pending' AND expires_at <= :now
""")

_COUNT_PENDING = text("""
    SELECT count(*) FROM pending_confirmations
     WHERE user_id = :user_id AND status = 'pending' AND expires_at > :now
""")


def enqueue(
    user_id: str,
    session: Session,
    *,
    agent: str,
    kind: PendingKind,
    target_table: str,
    payload: dict[str, Any],
    reason: PendingReason,
    confidence: float | None = None,
    source_event_id: int | None = None,
    now: datetime,
    ttl: timedelta = DEFAULT_TTL,
) -> Pending:
    """把一条"拿不准的东西"放进队列。

    `payload` 存的是 **agent 原本想写入的内容** —— 确认之后原样写进
    `target_table`。所以它必须是能直接落库的形状,不是给人看的描述文本;
    人看的那份由展示层从它渲染。两者混在一起,确认之后就没法机械地写入了。
    """
    if not payload:
        raise PendingError("payload 不能为空:确认之后就是照它写库的")
    if not target_table.strip():
        raise PendingError("target_table 不能为空:确认之后要写去哪都不知道")

    row = session.execute(
        _INSERT,
        {
            "user_id": user_id,
            "agent": agent,
            "kind": kind.value,
            "target_table": target_table,
            "payload": json.dumps(payload, ensure_ascii=False, default=str),
            "reason": reason.value,
            "confidence": confidence,
            "source_event_id": source_event_id,
            "expires_at": now + ttl,
        },
    ).one()
    return _to_pending(row)


def confirm(
    user_id: str,
    session: Session,
    *,
    pending_id: int,
    writer: Callable[[dict[str, Any]], Any],
    resolved_via: str,
    edited_payload: dict[str, Any] | None = None,
) -> Any | None:
    """确认一条,并**在同一个事务里**把它写进目标表。

    `writer` 收到最终要写入的内容(改过就是 `edited_payload`),它的返回值原样
    透传出去 —— 调用方多半要那个新建行的 id。

    返回 None 表示**这条没能被认领**:已经处理过了,或者已经过期。
    不抛异常,因为两个入口同时点确认是正常的用户行为,不是错误。

    `writer` 抛异常时整个事务回滚:状态改不成 confirmed,目标表也没写进去。
    **这正是要的** —— "确认了但没写进去"是这里唯一不能出现的状态。
    """
    status = PendingStatus.EDITED if edited_payload else PendingStatus.CONFIRMED
    row = session.execute(
        _CLAIM,
        {
            "user_id": user_id,
            "pending_id": pending_id,
            "status": status.value,
            "via": resolved_via,
            "resolved_payload": (
                json.dumps(edited_payload, ensure_ascii=False, default=str)
                if edited_payload
                else None
            ),
        },
    ).first()

    if row is None:
        # 已经被处理过或已过期。第二个入口拿到的是"已经处理过了",不是写第二遍
        log.info("待确认 %s 没能被认领:已处理或已过期", pending_id)
        return None

    return writer(edited_payload or dict(row.payload))


def reject(user_id: str, session: Session, *, pending_id: int, resolved_via: str) -> bool:
    """用户说这条不对。**记录保留不删** —— 它是评测集的负样本。"""
    row = session.execute(
        _CLAIM,
        {
            "user_id": user_id,
            "pending_id": pending_id,
            "status": PendingStatus.REJECTED.value,
            "via": resolved_via,
            "resolved_payload": None,
        },
    ).first()
    return row is not None


def list_pending(
    user_id: str, session: Session, *, now: datetime, limit: int = 50
) -> list[Pending]:
    """还等着人处理的。App 上那个待确认列表就是它。"""
    rows = session.execute(_LIST, {"user_id": user_id, "now": now, "limit": limit}).all()
    return [_to_pending(row) for row in rows]


def count_pending(user_id: str, session: Session, *, now: datetime) -> int:
    """积压了多少条。

    这个数字长期只涨不落,说明 agent 的判据太保守 —— 那不是安全,是把活儿
    全推给了用户,而用户会在某一天不再看这个列表。
    """
    return int(session.execute(_COUNT_PENDING, {"user_id": user_id, "now": now}).scalar_one())


def expire_overdue(user_id: str, session: Session, *, now: datetime) -> int:
    """把过期的标成 expired。返回条数。

    过期不删记录:和 rejected 一样,"用户当初没管这条"本身就是信息。
    """
    return session.execute(_EXPIRE, {"user_id": user_id, "now": now}).rowcount


def get(user_id: str, session: Session, *, pending_id: int) -> Pending | None:
    row = session.execute(_SELECT_ONE, {"user_id": user_id, "pending_id": pending_id}).first()
    return _to_pending(row) if row else None


def _to_pending(row) -> Pending:
    return Pending(
        id=row.id,
        agent=row.agent,
        kind=PendingKind(row.kind),
        target_table=row.target_table,
        payload=dict(row.payload or {}),
        reason=PendingReason(row.reason),
        confidence=float(row.confidence) if row.confidence is not None else None,
        source_event_id=row.source_event_id,
        status=PendingStatus(row.status),
        expires_at=row.expires_at,
    )
