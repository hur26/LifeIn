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


def digest_card(card: Card) -> dict:
    """把卡片压成形状:标题留着(它本来就是给人看的一行),正文只留长度。"""
    return {
        "title": card.title,
        "summary_len": len(card.summary),
        "sections": [{"heading": s.heading, "lines": len(s.lines)} for s in card.sections],
    }


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
) -> int:
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
                "payload_digest": json.dumps(digest_card(card), ensure_ascii=False),
                "delivered": delivered,
                "error": error,
            },
        ).scalar_one()
    )


def count_active_pushes_since(user_id: str, session: Session, *, since: datetime) -> int:
    """频率闸门的计数来源。只数真的送达了的 —— 发失败的不该占额度。"""
    return int(session.execute(_COUNT_TODAY, {"user_id": user_id, "since": since}).scalar_one())
