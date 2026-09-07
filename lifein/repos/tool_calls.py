"""`tool_calls` 的写入 —— `AuditSink` 的落库实现。

**审计写失败要能被发现,但不该让业务调用跟着失败。** 这两句话看着矛盾,
落地方式是:捕获异常并 `log.exception`,不往上抛。理由是审计记的是"已经
发生过的事",此刻抛异常不会让那件事没发生,只会让一次成功的工具调用看起来
像失败,进而触发重试 —— 而重试会把副作用做第二遍。

**但光捕获异常是不够的**,这是连真库跑一次才会发现的事:PostgreSQL 里
一条语句失败会把**整个事务**置为 aborted,之后同一事务里的任何语句都报
`current transaction is aborted`。也就是说吞掉异常之后,调用方看起来没事,
下一句写入却必然失败 —— 比直接抛出去还糟。

所以插入包在 `begin_nested()`(SAVEPOINT)里:失败只回滚到保存点,
外层事务毫发无损。`l2_needs_rollback` 那条约束是最可能触发它的场景 ——
网关已经先炸过一次,真走到这里说明有人绕过了网关。
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifein.governance.audit import ToolCallRecord

log = logging.getLogger(__name__)

_INSERT = text("""
    INSERT INTO tool_calls
        (user_id, agent, tool_name, level, args_digest, llm_fields_sent,
         result_status, duration_ms, prompt_tokens, completion_tokens,
         cost_cny, rollback_info)
    VALUES
        (:user_id, :agent, :tool_name, :level, CAST(:args_digest AS JSONB),
         :llm_fields_sent, :result_status, :duration_ms, :prompt_tokens,
         :completion_tokens, :cost_cny, CAST(:rollback_info AS JSONB))
""")


def record_tool_call(user_id: str, session: Session, entry: ToolCallRecord) -> None:
    """写一条审计。失败只记日志,不往上抛 —— 理由见模块文档。"""
    try:
        # SAVEPOINT:失败只回滚到这里,不把外层事务拖成 aborted
        with session.begin_nested():
            session.execute(
                _INSERT,
                {
                    "user_id": user_id,
                    "agent": entry.agent,
                    "tool_name": entry.tool_name,
                    "level": entry.level.value,
                    "args_digest": json.dumps(entry.args_digest, ensure_ascii=False, default=str),
                    "llm_fields_sent": list(entry.llm_fields_sent),
                    "result_status": entry.result_status,
                    "duration_ms": entry.duration_ms,
                    "prompt_tokens": entry.prompt_tokens,
                    "completion_tokens": entry.completion_tokens,
                    "cost_cny": entry.cost_cny,
                    "rollback_info": (
                        json.dumps(entry.rollback_info, ensure_ascii=False, default=str)
                        if entry.rollback_info is not None
                        else None
                    ),
                },
            )
    except Exception:  # noqa: BLE001
        log.exception(
            "审计写入失败:agent=%s tool=%s status=%s",
            entry.agent,
            entry.tool_name,
            entry.result_status,
        )


class PostgresAuditSink:
    """把 `Gateway` 的审计接到数据库上。

    持有 session 而不是每次新建:网关的调用发生在一个工作单元里,审计要和
    业务写入在同一个事务里 —— 否则会出现"业务回滚了,审计说做过"。
    """

    def __init__(self, user_id: str, session: Session) -> None:
        self._user_id = user_id
        self._session = session

    def record(self, entry: ToolCallRecord) -> None:
        record_tool_call(self._user_id, self._session, entry)
