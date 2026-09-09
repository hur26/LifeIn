"""审批队列(06 §2.8)。**L3 调用停在这里等人点。**

03 给 P3 的验收标准是**完成 20 次真实 L3 操作,零重复执行、零越权**,
而这张表就是那两个零的落点:

- **零越权** 靠 `l3_never_triggered_by_external` 那条 `CHECK`
  ([铁律 8](../../AGENTS.md#1-铁律))。提示注入即使骗过 agent,也写不进这张表
- **零重复执行** 靠两道:`UNIQUE (user_id, idempotency_key)` 挡住"提两次",
  状态机挡住"执行两次"

## 状态机只往一个方向走

```
pending ──approve()──→ approved ──mark_executed()──→ executed
   │                       │
   ├──reject()──→ rejected └──mark_failed()──→ failed
   └──expire()──→ expired
```

**每一步都带上"从什么状态来"**(`WHERE status = ...`),所以并发点两次、
回调重投、job 跑两遍,都只有第一次能改到行。这不是乐观锁的花招 ——
它是"零重复执行"唯一的实现方式:**判断和写入必须是同一条语句**,
先查再写中间那一瞬就是重复执行的全部空间。

## 幂等键由调用方给

同一个意图重复提出(你说了两遍"帮我回复老王")应该只有一条审批,
而"同一个意图"是什么,只有提出的那一层知道。这里不猜 ——
不给幂等键的话按内容算一个,但那是兜底,不是设计。

## 过期是安全属性,不是清理

默认 24 小时(06 §2.8)。过期不是为了让表干净,是为了**防止几天后误点**:
一条三天前提的"帮我回复老王"现在点同意,发出去的内容早就不合时宜了,
而卡片上看不出这一点。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifein.models.normalized import Trust

log = logging.getLogger(__name__)

DEFAULT_TTL = timedelta(hours=24)
"""默认多久过期。**比待确认那边的 30 天短得多**,理由见模块开头。"""


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    """人点了同意,**但还没执行**。执行由 job 去做(见 AGENTS §9 第 7 片)。"""

    EXECUTING = "executing"
    """执行 job 已经认领,**正要动外部世界**。

    这个状态存在的全部理由是"不要发两条":认领是一条带条件的 UPDATE,
    第二个执行者拿到 0 行,**在发出去之前**就知道自己白跑了。

    **卡在这里的行不自动重试。** 它的含义是"我们开始发了,但不知道发出去
    没有" —— 重试可能发第二条,放着不管可能一条都没发。那是唯一不能替用户
    猜的情况,所以由人看一眼(`admin approvals` 里看得到)。
    """

    REJECTED = "rejected"
    EXECUTED = "executed"
    EXPIRED = "expired"
    FAILED = "failed"
    """执行时出错了。**和 rejected 分开** —— 一个是你不要,一个是没做成,
    而后者可能还要重试。"""


class ApprovalError(ValueError):
    """用法错了。都在动数据之前抛。"""


@dataclass(frozen=True)
class Approval:
    id: int
    agent: str
    tool_name: str
    tool_args: dict[str, Any]
    preview_text: str
    idempotency_key: str
    status: ApprovalStatus
    expires_at: datetime
    source_event_id: int | None = None
    approved_at: datetime | None = None
    started_at: datetime | None = None
    """认领去执行的那一刻。**卡住的那些靠它才判得出来** ——
    `approved_at` 是"人点同意"的时刻,和执行差着一整个调度周期。"""

    executed_at: datetime | None = None
    result: dict[str, Any] | None = None

    def is_open(self, *, now: datetime) -> bool:
        return self.status is ApprovalStatus.PENDING and self.expires_at > now


_COLUMNS = """
    id, agent, tool_name, tool_args, preview_text, idempotency_key, status,
    expires_at, source_event_id, approved_at, started_at, executed_at, result
"""

_INSERT = text(f"""
    INSERT INTO approvals
        (user_id, agent, tool_name, tool_args, preview_text, idempotency_key,
         trigger_trust, source_event_id, expires_at)
    VALUES
        (:user_id, :agent, :tool_name, CAST(:tool_args AS JSONB), :preview_text,
         :idempotency_key, :trigger_trust, :source_event_id, :expires_at)
    -- 同一个意图提两次只留一条。**这是"零重复执行"的第一道** ——
    -- 第二道是下面那些 UPDATE 里的 WHERE status
    ON CONFLICT (user_id, idempotency_key) DO NOTHING
    RETURNING {_COLUMNS}
""")

_SELECT_BY_KEY = text(f"""
    SELECT {_COLUMNS} FROM approvals
     WHERE user_id = :user_id AND idempotency_key = :idempotency_key
""")

_SELECT_BY_ID = text(f"SELECT {_COLUMNS} FROM approvals WHERE user_id = :user_id AND id = :id")

_TRANSITION = text(f"""
    UPDATE approvals
       SET status = :to_status,
           approved_at = CASE WHEN :to_status = 'approved' THEN :now ELSE approved_at END,
           executed_at = CASE
               WHEN :to_status IN ('executed', 'failed') THEN :now ELSE executed_at END,
           result = COALESCE(CAST(:result AS JSONB), result)
     WHERE user_id = :user_id
       AND id = :id
       -- **判断和写入是同一条语句。** 先查再写,中间那一瞬就是重复执行的
       -- 全部空间 —— 而并发点两次、回调重投、job 跑两遍都会落在那一瞬里
       AND status = :from_status
       AND (:to_status <> 'approved' OR expires_at > :now)
 RETURNING {_COLUMNS}
""")

_LIST_OPEN = text(f"""
    SELECT {_COLUMNS} FROM approvals
     WHERE user_id = :user_id AND status = 'pending' AND expires_at > :now
     ORDER BY created_at
     LIMIT :limit
""")

_LIST_READY = text(f"""
    SELECT {_COLUMNS} FROM approvals
     WHERE user_id = :user_id AND status = 'approved'
     ORDER BY approved_at
     LIMIT :limit
""")

_CLAIM = text(f"""
    UPDATE approvals
       SET status = 'executing',
           started_at = :now
     WHERE user_id = :user_id
       AND id = :id
       AND status = 'approved'
 RETURNING {_COLUMNS}
""")
"""认领去执行。**判断和写入在同一条语句里。**

先读再写会留下一个窗口,而这个工具动的是外部世界 —— 那个窗口里发出去的
第二条消息撤不回来。
"""

_STUCK = text(f"""
    SELECT {_COLUMNS} FROM approvals
     WHERE user_id = :user_id
       AND status = 'executing'
       AND started_at <= :cutoff
     ORDER BY started_at
     LIMIT :limit
""")
"""卡在执行中的那些。

**按 `started_at` 判,不是 `approved_at`** —— 后者是"人点同意"的时刻,
和执行差着一整个调度周期,拿它判会把刚认领的那条也算成卡住的。
"""

_EXPIRE = text("""
    UPDATE approvals
       SET status = 'expired'
     WHERE user_id = :user_id AND status = 'pending' AND expires_at <= :now
""")

_RECENT = text(f"""
    SELECT {_COLUMNS} FROM approvals
     WHERE user_id = :user_id
     ORDER BY created_at DESC
     LIMIT :limit
""")


def enqueue(
    user_id: str,
    session: Session,
    *,
    agent: str,
    tool_name: str,
    tool_args: dict[str, Any],
    preview_text: str,
    trust: Trust,
    now: datetime,
    idempotency_key: str | None = None,
    source_event_id: int | None = None,
    ttl: timedelta = DEFAULT_TTL,
) -> Approval:
    """把一次 L3 调用挂起来等人点。**同一个幂等键重复提只有一条。**

    `trust` 不是 `user_input` 时直接抛 —— 库上那条 `CHECK` 也会拦,
    但在这里先拦一道是为了**报得出人话**:数据库抛的是约束名,
    而那个名字不解释"为什么外部内容不能触发 L3"。
    """
    if trust is not Trust.USER_INPUT:
        # 铁律 8。网关那边也有一道,两道都在是刻意的
        raise ApprovalError(
            f"L3 不接受由外部内容触发(trust={trust.value})。"
            "邮件、群消息、通知里的任何一句话都不能启动一次代发消息"
        )
    if not preview_text.strip():
        # 预览是空的话,人点同意时是在赌 —— 而 03 的退出条件里有这一条
        raise ApprovalError("审批必须带一句人话的预览,不能只有 tool_args")

    key = idempotency_key or _content_key(agent=agent, tool_name=tool_name, args=tool_args)
    row = session.execute(
        _INSERT,
        {
            "user_id": user_id,
            "agent": agent,
            "tool_name": tool_name,
            "tool_args": json.dumps(tool_args, ensure_ascii=False, default=str),
            "preview_text": preview_text.strip(),
            "idempotency_key": key,
            "trigger_trust": trust.value,
            "source_event_id": source_event_id,
            "expires_at": now + ttl,
        },
    ).first()

    if row is None:
        # 已经提过了。**返回那一条而不是报错** —— 重复提出是正常的用户行为
        # (你说了两遍"帮我回复老王"),而报错会让调用方以为出了问题
        existing = session.execute(
            _SELECT_BY_KEY, {"user_id": user_id, "idempotency_key": key}
        ).one()
        log.info("审批已存在,不重复创建:%s", key)
        return _to_approval(existing)
    return _to_approval(row)


def get(user_id: str, session: Session, *, approval_id: int) -> Approval | None:
    row = session.execute(_SELECT_BY_ID, {"user_id": user_id, "id": approval_id}).first()
    return _to_approval(row) if row else None


def approve(user_id: str, session: Session, *, approval_id: int, now: datetime) -> Approval | None:
    """人点了同意。**只把状态改成 approved,不执行。**

    执行放在 job 里,不放在这里:回调有超时,而超时重投会**再执行一次** ——
    03 那条"零重复执行"就是这么破的(AGENTS §9 第 7 片)。

    返回 None 表示这一条不在 `pending`,或者已经过期 ——
    两种都不是错误:并发点两次、点一条昨天的,都是正常的用户行为。
    """
    return _transition(
        user_id,
        session,
        approval_id=approval_id,
        from_status=ApprovalStatus.PENDING,
        to_status=ApprovalStatus.APPROVED,
        now=now,
    )


def reject(user_id: str, session: Session, *, approval_id: int, now: datetime) -> Approval | None:
    """人点了拒绝。**不删记录** —— 拒绝过什么是判断预览做得好不好的原料。"""
    return _transition(
        user_id,
        session,
        approval_id=approval_id,
        from_status=ApprovalStatus.PENDING,
        to_status=ApprovalStatus.REJECTED,
        now=now,
    )


def mark_executed(
    user_id: str,
    session: Session,
    *,
    approval_id: int,
    now: datetime,
    result: dict[str, Any] | None = None,
) -> Approval | None:
    """做完了。**从 `executing` 出发。**

    原来是从 `approved` 出发,而且没有单独的认领步骤 —— 理由写着
    "让写结果这一步自己带上 WHERE status='approved',一处就够了"。
    那句话对"只有一条记录会变成 executed"是成立的,**但它不能阻止消息被发两次**:
    两个执行者都从 `approved` 读到同一条,都执行,都发出去,只是其中一个
    在写结果时才发现自己是第二个。而那时消息已经出去了。

    见 `claim_for_execution`。
    """
    return _transition(
        user_id,
        session,
        approval_id=approval_id,
        from_status=ApprovalStatus.EXECUTING,
        to_status=ApprovalStatus.EXECUTED,
        now=now,
        result=result,
    )


def mark_failed(
    user_id: str,
    session: Session,
    *,
    approval_id: int,
    now: datetime,
    error: str,
) -> Approval | None:
    """没做成。**和 rejected 分开**:一个是你不要,一个是没做成。

    后者可能还要重试,而重试要先有人看过 —— 所以它不会自己回到 `approved`。

    从 `executing` 出发:执行前就判失败的那些(工具找不到、入参对不上)
    也要先认领再标失败,否则那条会留在 `approved` 里,下一轮再试一遍、
    再失败一遍,而每一轮都发一封告警。
    """
    return _transition(
        user_id,
        session,
        approval_id=approval_id,
        from_status=ApprovalStatus.EXECUTING,
        to_status=ApprovalStatus.FAILED,
        now=now,
        result={"error": error},
    )


def list_open(
    user_id: str, session: Session, *, now: datetime, limit: int = 50
) -> list[Approval]:
    """等着人点的那些。**过期的不算** —— 卡片上不该出现一条点不动的东西。"""
    rows = session.execute(
        _LIST_OPEN, {"user_id": user_id, "now": now, "limit": limit}
    ).all()
    return [_to_approval(row) for row in rows]


def list_ready(user_id: str, session: Session, *, limit: int = 50) -> list[Approval]:
    """点过同意、还没执行的那些。执行 job 读它。"""
    rows = session.execute(_LIST_READY, {"user_id": user_id, "limit": limit}).all()
    return [_to_approval(row) for row in rows]


def claim_for_execution(
    user_id: str, session: Session, *, approval_id: int, now: datetime
) -> Approval | None:
    """认领一条去执行。**拿不到就别执行** —— 已经有人在做了。

    这是"零重复执行"(03 的 P3 退出条件)真正落地的地方。它必须发生在
    调用工具**之前**:动外部世界之后再发现自己是第二个,已经晚了。
    """
    row = session.execute(
        _CLAIM, {"user_id": user_id, "id": approval_id, "now": now}
    ).first()
    if row is None:
        log.info("审批 %s 认领不到:别人已经在执行,或状态变了", approval_id)
        return None
    return _to_approval(row)


def list_stuck(
    user_id: str, session: Session, *, cutoff: datetime, limit: int = 20
) -> list[Approval]:
    """卡在 `executing` 的那些 —— "开始发了,但不知道发出去没有"。

    **不自动重试,也不自动标失败。** 重试可能发第二条,标失败会让人以为
    没发出去。这个函数的用途只有一个:让人看见它。
    """
    rows = session.execute(
        _STUCK, {"user_id": user_id, "cutoff": cutoff, "limit": limit}
    ).all()
    return [_to_approval(row) for row in rows]


def list_recent(user_id: str, session: Session, *, limit: int = 20) -> list[Approval]:
    """最近的几条,不管什么状态。`admin approvals` 和演练记录读它。"""
    rows = session.execute(_RECENT, {"user_id": user_id, "limit": limit}).all()
    return [_to_approval(row) for row in rows]


def expire_overdue(user_id: str, session: Session, *, now: datetime) -> int:
    """把过了期的标掉。返回改了几条。

    **这是安全动作,不是清理动作。** 一条三天前提的"帮我回复老王"现在点同意,
    发出去的内容早就不合时宜了,而卡片上看不出这一点。
    """
    return session.execute(_EXPIRE, {"user_id": user_id, "now": now}).rowcount


def _transition(
    user_id: str,
    session: Session,
    *,
    approval_id: int,
    from_status: ApprovalStatus,
    to_status: ApprovalStatus,
    now: datetime,
    result: dict[str, Any] | None = None,
) -> Approval | None:
    row = session.execute(
        _TRANSITION,
        {
            "user_id": user_id,
            "id": approval_id,
            "from_status": from_status.value,
            "to_status": to_status.value,
            "now": now,
            "result": json.dumps(result, ensure_ascii=False, default=str) if result else None,
        },
    ).first()
    if row is None:
        log.info(
            "审批 %s 没能从 %s 变成 %s(状态已变或已过期)",
            approval_id,
            from_status.value,
            to_status.value,
        )
        return None
    return _to_approval(row)


def _content_key(*, agent: str, tool_name: str, args: dict[str, Any]) -> str:
    """没给幂等键时按内容算一个。**这是兜底,不是设计。**

    "同一个意图"是什么只有提出的那一层知道 —— 比如"回复老王"提两次该算一次,
    哪怕两次的措辞不一样。按内容算做不到那件事,只能挡住一字不差的重复。
    """
    payload = json.dumps(
        {"agent": agent, "tool": tool_name, "args": args},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return f"auto:{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def _to_approval(row) -> Approval:
    return Approval(
        id=row.id,
        agent=row.agent,
        tool_name=row.tool_name,
        tool_args=dict(row.tool_args or {}),
        preview_text=row.preview_text,
        idempotency_key=row.idempotency_key,
        status=ApprovalStatus(row.status),
        expires_at=row.expires_at,
        source_event_id=row.source_event_id,
        approved_at=row.approved_at,
        started_at=row.started_at,
        executed_at=row.executed_at,
        result=dict(row.result) if row.result else None,
    )
