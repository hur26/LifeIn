"""审计记录与入参摘要。

AGENTS.md §3:**审计日志记入参摘要,不记原文。** 日志本身是数据集中点 ——
把原文记进去,等于在库里又建了一份全量副本,而且是不带加密的那份(R10)。

摘要保留的是"形状":字段名、类型、长度、内容哈希前 8 位。
排查问题时你需要知道的通常是"这次和上次传的是不是同一个东西",哈希够用;
真需要看内容,去 raw_events 按 source_event_id 找原始事件。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel

from lifein.governance.registry import ToolLevel


def _digest_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        # 布尔本身不泄露内容,留着值对排查最有用
        return {"type": "bool", "value": value}
    if isinstance(value, int | float | Decimal):
        # 不留值:金额、条数这类数字本身就是内容
        return {"type": type(value).__name__}
    if isinstance(value, str | bytes):
        raw = value.encode() if isinstance(value, str) else value
        return {
            "type": "str" if isinstance(value, str) else "bytes",
            "len": len(value),
            "sha256_8": hashlib.sha256(raw).hexdigest()[:8],
        }
    if isinstance(value, list | tuple | set):
        return {"type": "list", "len": len(value)}
    if isinstance(value, dict):
        return {"type": "dict", "keys": sorted(str(k) for k in value)}
    if isinstance(value, BaseModel):
        return {"type": "model", "fields": sorted(value.model_dump().keys())}
    return {"type": type(value).__name__}


def digest_args(args: BaseModel) -> dict[str, Any]:
    """把工具入参压成"形状"。字段名保留,值不保留。"""
    return {name: _digest_value(value) for name, value in args.model_dump().items()}


@dataclass
class ToolCallRecord:
    """一次工具调用的审计记录,字段对应 06 §2.9 的 tool_calls 表。"""

    user_id: str
    agent: str
    tool_name: str
    level: ToolLevel
    args_digest: dict[str, Any]
    result_status: str
    """allowed / denied / approval_required / error"""

    duration_ms: int | None = None
    llm_fields_sent: list[str] = field(default_factory=list)
    """发给外部模型的**字段名**,不是内容。让"我到底把什么发出去了"是个能回答的
    问题(R12)。工具自己不调 LLM 时留空。"""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_cny: Decimal | None = None
    rollback_info: dict[str, Any] | None = None
    """L2 必填。对应表上的 l2_needs_rollback 约束 —— 没有它这条记录写不进去,
    也就等于那次调用不该发生。"""

    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AuditSink(Protocol):
    """审计出口。P0 落库,测试用内存实现。

    它故意不返回任何东西:审计失败不该影响调用结果的语义,但必须能被发现 ——
    落库实现里写失败要告警,不是吞掉。
    """

    def record(self, entry: ToolCallRecord) -> None: ...


class InMemoryAuditSink:
    """测试与影子模式用。"""

    def __init__(self) -> None:
        self.entries: list[ToolCallRecord] = []

    def record(self, entry: ToolCallRecord) -> None:
        self.entries.append(entry)

    def by_status(self, status: str) -> list[ToolCallRecord]:
        return [e for e in self.entries if e.result_status == status]
