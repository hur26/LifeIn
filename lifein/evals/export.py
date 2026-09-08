"""从库里**攒**负样本,不是编负样本(06 §7.3)。

[06 §2.7](../../docs/06-data-model.md#27-pending_confirmations--统一待确认队列) 说
`rejected` 与 `expired` 的记录永不删除,理由就是这里 ——
**用户拒绝过什么,正是这个 agent 最该学会不做的事。**
`facts.negated_by_user` 同理([R7](../../docs/05-risks.md#r7--记忆污染))。

那两句话在这个文件之前只是一句话。有了它,"永不删除"才真的换来了东西。

**导出来的不能直接用。** 用户拒绝的原因不总是"agent 错了",也可能是
"这事我自己记得" —— 后一种不该进负样本。所以这里只产出候选,
合并进 `evals/` 之前要人看一遍(命令会把这句话再说一遍)。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifein.evals import ABSENT, Case, Expectation
from lifein.repos import raw_events

log = logging.getLogger(__name__)

_REJECTED_PENDINGS = text("""
    SELECT id, agent, payload, reason, status, source_event_id
      FROM pending_confirmations
     WHERE user_id = :user_id
       AND agent = :agent
       AND status IN ('rejected', 'expired')
       AND source_event_id IS NOT NULL
     ORDER BY resolved_at DESC NULLS LAST
     LIMIT :limit
""")

_NEGATED_FACTS = text("""
    SELECT id, statement, provenance, created_by_agent
      FROM facts
     WHERE user_id = :user_id
       AND negated_by_user = true
       AND created_by_agent <> 'user'
     ORDER BY created_at DESC
     LIMIT :limit
""")


def export_planner_negatives(
    user_id: str, session: Session, *, now: datetime, limit: int = 50
) -> list[Case]:
    """拒绝过的待确认 → planner 的负样本。

    期望是 `items` 为空:**那件事当初就不该被提出来**。
    比"应该走待确认"更严格是有意的 —— 用户已经说了不对,那么最好的行为
    是根本不提,而不是再问一次。
    """
    rows = session.execute(
        _REJECTED_PENDINGS, {"user_id": user_id, "agent": "planner", "limit": limit}
    ).all()

    cases: list[Case] = []
    for row in rows:
        stored = raw_events.fetch_stored_between  # 占位,下面用按 id 取
        del stored
        events = _events_for(user_id, session, [row.source_event_id])
        if not events:
            # 来源事件被删了(不该发生,raw_events 只追加)。跳过而不是造一个空输入
            log.warning("待确认 %s 的来源事件不在了,跳过", row.id)
            continue

        title = (row.payload or {}).get("title", "")
        cases.append(
            Case(
                id=f"planner-rejected-{row.id}",
                input={"events": events, "now": now.isoformat()},
                expect=[Expectation(path="items", op=ABSENT)],
                note=(
                    f"用户拒绝过这条({row.status},当初理由 {row.reason}):{title}。"
                    "确认它当初不该被提出来再合并进来 —— 也可能只是"这事我自己记得""
                ),
            )
        )
    return cases


def export_memory_negatives(
    user_id: str, session: Session, *, limit: int = 50
) -> list[Case]:
    """否定过的事实 → memory 的负样本。

    期望同样是"不该再推出来"。这条比 planner 那条更要紧:
    被否定的事实如果明天又冒出来,**比第一次冒出来更伤** ——
    那说明"否定"这个动作没有用(facts.py 开头那段)。
    """
    rows = session.execute(_NEGATED_FACTS, {"user_id": user_id, "limit": limit}).all()

    cases: list[Case] = []
    for row in rows:
        events = _events_for(user_id, session, list(row.provenance or []))
        if not events:
            log.warning("事实 %s 的来源事件不在了,跳过", row.id)
            continue
        cases.append(
            Case(
                id=f"memory-negated-{row.id}",
                input={"events": events, "own_identifiers": []},
                expect=[Expectation(path="facts", op=ABSENT)],
                note=f"用户否定过这条:{row.statement}",
            )
        )
    return cases


def _events_for(user_id: str, session: Session, event_ids: list[int]) -> list[dict]:
    """把 `raw_events` 的几行还原成 agent 输入里的 `events`。

    只取归一化好的那部分:**评测集里存的是归一化之后的形状**,
    不是原始 payload。原始 payload 里有群友的原话,而那份东西过了保留期
    就该没有了(R10)—— 评测集不该把它复活。
    """
    if not event_ids:
        return []
    rows = session.execute(
        text(
            "SELECT id, normalized FROM raw_events"
            " WHERE user_id = :user_id AND id = ANY(:ids) AND normalized IS NOT NULL"
        ),
        {"user_id": user_id, "ids": event_ids},
    ).all()
    return [{"event_id": row.id, "event": row.normalized} for row in rows]
