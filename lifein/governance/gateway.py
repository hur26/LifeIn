"""分权网关 —— 所有工具调用的唯一入口。

架构 §1:**能力层的任何工具都不允许被编排层直接调用。** 这条靠两件事实现:
注册表不导出函数引用(registry.py),以及这里是拿到 `ToolSpec.func` 的唯一地方。

网关按顺序过五道:

    1. 工具存在吗           不存在 = 未声明等级 = 按 L3 拒绝(铁律 3)
    2. 这个 agent 能调它吗   (agent, tool) 双重门(ADR-013)
    3. 入参合法吗           Pydantic 校验,失败在执行前
    4. 等级决定路径          L1/L2 执行,L3 一律进审批队列
    5. 无论结果如何都记审计    包括被拒的

第 4 道里最要紧的是 L3 那条:**触发它的内容必须是 user_input**。
外部内容(邮件、群消息、通知)永远不能直接触发 L3,这是铁律 8。
数据库层面 approvals 表有 CHECK,这里是它前面那道 —— 两道都有是刻意的,
因为一道会被绕过,而绕过的方式往往是"我这里特殊"。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy.orm import Session

from lifein.agents.contract import get_agent
from lifein.governance.audit import AuditSink, ToolCallRecord, digest_args
from lifein.governance.registry import (
    ToolContext,
    ToolLevel,
    ToolSpec,
    UnknownTool,
    get_tool,
)
from lifein.models.normalized import Trust


class Denied(RuntimeError):
    """这次调用不该发生。已记审计。"""


class ApprovalRequired(Exception):
    """L3 调用已进审批队列,等人点。**不是错误** —— 是这条路径的正常终点。"""

    def __init__(self, approval_id: str, preview_text: str) -> None:
        super().__init__(f"需要人工审批:{preview_text}")
        self.approval_id = approval_id
        self.preview_text = preview_text


@dataclass(frozen=True)
class CallContext:
    """一次调用的来源。

    `trust` 说的是**触发这次调用的内容**可不可信,不是入参本身。
    群消息里说"帮我给老板发条消息" —— 入参再规整,trust 也是 external。
    """

    user_id: str
    agent: str
    trust: Trust
    source_event_id: int | None = None
    session: Session | None = None
    """要碰库的工具用它。**调用方的事务,不是工具自己开的**(见 `ToolContext`)。

    `agent` 说的是"这次调用代表哪个 agent",不是"谁写的这行代码"。
    调度层替 agent 发起调用时照样填那个 agent 的名字 ——
    双重门校验的是职责,不是调用栈。
    """

    idempotency_key: str | None = None
    """L3 用:**"同一个意图"是什么,只有提出的那一层知道。**

    问答 agent 知道"回复老王那条消息"是同一件事,哪怕你两次说法不同;
    而网关和 `approvals` 都不知道。不给的话按内容算一个兜底的,
    但那只挡得住一字不差的重复 —— 于是你换个说法再说一遍,就会多一条审批。
    """


@dataclass(frozen=True)
class ToolOutcome:
    """L2 工具的返回值。

    `rollback` 不是可选的:没有它,tool_calls 的 l2_needs_rollback 约束
    会让审计写不进去,而写不进审计的调用等于不该发生。
    """

    value: Any
    rollback: dict[str, Any]


class ApprovalQueue(Protocol):
    def enqueue(
        self,
        *,
        ctx: CallContext,
        spec: ToolSpec,
        args: Any,
        preview_text: str,
    ) -> str: ...


class Gateway:
    def __init__(self, audit: AuditSink, approvals: ApprovalQueue | None = None) -> None:
        self._audit = audit
        self._approvals = approvals

    def call(self, ctx: CallContext, tool_name: str, args: Mapping[str, Any]) -> Any:
        try:
            spec = get_tool(tool_name)
        except UnknownTool:
            # 连等级都不知道,只能按最严的处理
            self._record(ctx, tool_name, ToolLevel.L3, {}, "denied")
            raise Denied(f"未注册的工具:{tool_name}") from None

        allowed = get_agent(ctx.agent).tools
        if tool_name not in allowed:
            # 工具是 L1 也不代表任何 agent 都能调它
            self._record(ctx, tool_name, spec.level, {}, "denied")
            raise Denied(f"agent {ctx.agent} 的白名单里没有 {tool_name}")

        try:
            parsed = spec.args_model(**dict(args))
        except ValidationError as exc:
            self._record(ctx, tool_name, spec.level, {}, "denied")
            raise Denied(f"{tool_name} 入参不合法:{exc.error_count()} 处") from exc

        digest = digest_args(parsed)

        if spec.level is ToolLevel.L3:
            return self._handle_l3(ctx, spec, parsed, digest)
        return self._execute(ctx, spec, parsed, digest)

    # ---------- L3 ----------

    def _handle_l3(self, ctx: CallContext, spec: ToolSpec, parsed: Any, digest: dict) -> Any:
        if ctx.trust is not Trust.USER_INPUT:
            # 铁律 8。提示注入能骗过 agent,骗不过这一行
            self._record(ctx, spec.name, spec.level, digest, "denied")
            raise Denied(f"{spec.name} 是 L3,不接受由外部内容触发(trust={ctx.trust})")

        if self._approvals is None:
            self._record(ctx, spec.name, spec.level, digest, "denied")
            raise Denied(f"{spec.name} 是 L3,但没有配置审批队列")

        # **卡片上写的是这一次要做什么,不是这个工具一般做什么。**
        # 03 的退出条件:"你自己不敢点'同意' → 预览做得不够清楚"
        preview = spec.preview_for(parsed)
        approval_id = self._approvals.enqueue(
            ctx=ctx, spec=spec, args=parsed, preview_text=preview
        )
        self._record(ctx, spec.name, spec.level, digest, "approval_required")
        raise ApprovalRequired(approval_id, preview)

    # ---------- L1 / L2 ----------

    def _execute(self, ctx: CallContext, spec: ToolSpec, parsed: Any, digest: dict) -> Any:
        started = time.perf_counter()
        try:
            result = spec.func(parsed, ToolContext(user_id=ctx.user_id, session=ctx.session))
        except Exception:
            self._record(ctx, spec.name, spec.level, digest, "error", self._elapsed_ms(started))
            raise

        elapsed = self._elapsed_ms(started)

        if spec.level is ToolLevel.L2:
            if not isinstance(result, ToolOutcome):
                # 副作用已经发生了,拦不住 —— 但必须记成 error 并炸出来,
                # 因为没有回滚信息的 L2 调用是不可收拾的状态。
                self._record(ctx, spec.name, spec.level, digest, "error", elapsed)
                raise Denied(f"{spec.name} 是 L2,必须返回 ToolOutcome(带 rollback)")
            self._record(ctx, spec.name, spec.level, digest, "allowed", elapsed, result.rollback)
            return result.value

        self._record(ctx, spec.name, spec.level, digest, "allowed", elapsed)
        return result.value if isinstance(result, ToolOutcome) else result

    # ---------- 审计 ----------

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    def _record(
        self,
        ctx: CallContext,
        tool_name: str,
        level: ToolLevel,
        digest: dict,
        status: str,
        duration_ms: int | None = None,
        rollback: dict | None = None,
    ) -> None:
        self._audit.record(
            ToolCallRecord(
                user_id=ctx.user_id,
                agent=ctx.agent,
                tool_name=tool_name,
                level=level,
                args_digest=digest,
                result_status=status,
                duration_ms=duration_ms,
                rollback_info=rollback,
            )
        )
