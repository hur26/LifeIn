"""`push_log` 的读写。

它有两个用途,别只当成日志:

**频率闸门靠它计数。** 产品定义 §5 那条硬上限"非用户主动触发的推送每天
不超过 3 条",唯一的计数来源就是这张表。闸门本身在 P1 做,但计数从 P0
就得是准的 —— 否则 P1 上线那天没有历史可依。

**影子模式靠它记录。** `mode='shadow'` 的记录表示"这条本来会推,但没推"。
新规则先跑一周影子再决定转不转 active,靠的就是把这两种记录放在同一张表里
比对。

`payload_digest` 记摘要不记原文 —— 和审计一个道理,日志本身是数据集中点。

**用户主动触发的回复不写这里。** 闸门管的是非用户主动触发的那些推送;
把问答的回复也记进来,等于你多问几句就把当天的摘要额度吃光了
(见 `jobs/qa_reply.py`)。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifein.channels.base import Card

log = logging.getLogger(__name__)

_INSERT = text("""
    INSERT INTO push_log
        (user_id, rule_id, channel, mode, payload_digest, delivered, error)
    VALUES
        (:user_id, :rule_id, :channel, :mode, CAST(:payload_digest AS JSONB),
         :delivered, :error)
    RETURNING id
""")

_COUNT_TODAY = text("""
    SELECT count(*)
      FROM push_log
     WHERE user_id = :user_id
       AND mode = 'active'
       AND delivered = true
       AND created_at >= :since
""")


def digest_card(card: Card, *, dedup_key: str | None = None) -> dict:
    """把卡片压成形状:标题留着(它本来就是给人看的一行),正文只留长度。

    `dedup_key` 是"这条推送针对的是哪个东西"(比如某条日程的 id)。
    提醒类规则每十几分钟扫一次,没有它就会把同一场会提醒到开完为止。
    放进 digest 而不是加一列:它是这条推送的属性,而 digest 本来就是
    "这条推送长什么样"的地方。
    """
    digest = {
        "title": card.title,
        "summary_len": len(card.summary),
        "sections": [{"heading": s.heading, "lines": len(s.lines)} for s in card.sections],
    }
    if dedup_key:
        digest["dedup_key"] = dedup_key
    return digest


def record_push(
    user_id: str,
    session: Session,
    *,
    channel: str,
    mode: str,
    card: Card,
    delivered: bool,
    rule_id: str | None = None,
    error: str | None = None,
    dedup_key: str | None = None,
    extra: dict | None = None,
) -> int:
    """写一条推送记录。

    `extra` 会并进 `payload_digest`,用来记"为什么没推" ——
    被闸门拦下和规则处在影子模式都会留下 shadow 行,而那两件事对
    "误报率算多少"的意义完全不同,不分开的话影子期的统计就是虚的。
    """
    if mode not in {"shadow", "active"}:
        raise ValueError(f"mode 只能是 shadow 或 active,给的是 {mode!r}")
    return int(
        session.execute(
            _INSERT,
            {
                "user_id": user_id,
                "rule_id": rule_id,
                "channel": channel,
                "mode": mode,
                "payload_digest": json.dumps(
                    {**digest_card(card, dedup_key=dedup_key), **(extra or {})},
                    ensure_ascii=False,
                ),
                "delivered": delivered,
                "error": error,
            },
        ).scalar_one()
    )


_WAS_PUSHED = text("""
    SELECT 1
      FROM push_log
     WHERE user_id = :user_id
       AND rule_id = :rule_id
       AND payload_digest ->> 'dedup_key' = :dedup_key
       AND created_at >= :since
     LIMIT 1
""")


@dataclass(frozen=True)
class PushRecord:
    """一条推送记录里**给人看的那部分**。

    影子期的复盘全靠它:标题 + 时间 + 是哪条规则,足够判断"这条要是真发出来,
    是不是误报"。`payload_digest` 里本来就只有标题和长度(digest_card),
    所以这里读不到正文 —— 那是有意的,日志本身是数据集中点。
    """

    id: int
    rule_id: str | None
    channel: str
    mode: str
    title: str
    dedup_key: str | None
    """这条推送针对的是哪个东西(日程提醒里就是那条 `todos.id`)。

    **复盘时靠它去把内容解出来** —— 日志里不存正文(AGENTS §3:审计记摘要不记原文),
    所以"这条提醒说的是哪件事"是在读的时候现查的,不是写的时候存下来的。
    """

    delivered: bool
    error: str | None
    created_at: datetime


_LIST_SINCE = text("""
    SELECT id, rule_id, channel, mode, payload_digest, delivered, error, created_at
      FROM push_log
     WHERE user_id = :user_id
       AND created_at >= :since
       AND (CAST(:mode AS TEXT) IS NULL OR mode = CAST(:mode AS TEXT))
       AND (CAST(:rule_id AS TEXT) IS NULL OR rule_id = CAST(:rule_id AS TEXT))
     ORDER BY created_at DESC
     LIMIT :limit
""")


def list_since(
    user_id: str,
    session: Session,
    *,
    since: datetime,
    mode: str | None = None,
    rule_id: str | None = None,
    limit: int = 200,
) -> list[PushRecord]:
    """列出一段时间内的推送记录。**影子期复盘读的就是它**。

    03 要求"先跑一周影子模式统计,再决定是否转 active",而统计的前提是
    看得见 —— 在这个函数之前,影子记录只进得去出不来。
    """
    rows = session.execute(
        _LIST_SINCE,
        {
            "user_id": user_id,
            "since": since,
            "mode": mode,
            "rule_id": rule_id,
            "limit": limit,
        },
    ).all()
    return [
        PushRecord(
            id=row.id,
            rule_id=row.rule_id,
            channel=row.channel,
            mode=row.mode,
            title=str((row.payload_digest or {}).get("title", "(没有标题)")),
            dedup_key=(row.payload_digest or {}).get("dedup_key"),
            delivered=row.delivered,
            error=row.error,
            created_at=row.created_at,
        )
        for row in rows
    ]


def was_pushed_since(
    user_id: str,
    session: Session,
    *,
    rule_id: str,
    dedup_key: str,
    since: datetime,
) -> bool:
    """这条东西最近有没有被这条规则推过(或影子记录过)。

    **shadow 的记录也算。** 影子期要统计"这条规则会推多少次",重复计数会让
    误报率看起来比实际低,而那正是决定要不要转 active 的那个数。
    """
    row = session.execute(
        _WAS_PUSHED,
        {"user_id": user_id, "rule_id": rule_id, "dedup_key": dedup_key, "since": since},
    ).first()
    return row is not None


def count_active_pushes_since(user_id: str, session: Session, *, since: datetime) -> int:
    """频率闸门的计数来源。只数真的送达了的 —— 发失败的不该占额度。"""
    return int(session.execute(_COUNT_TODAY, {"user_id": user_id, "since": since}).scalar_one())
