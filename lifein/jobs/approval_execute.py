"""审批执行 job(P3 第 7 片)。**点过同意的那些,在这里真的发生。**

    approvals(status=approved) → 执行工具 → executed / failed

## 为什么执行不在回调里

企微的回调有超时,而"代发消息"要等对面的接口几秒钟。回调里做的话,
**超时重投会再执行一次** —— 而 03 那条"零重复执行"就是这么破的,
而且破得很安静:对面收到两条一样的消息,你这边的日志看起来一切正常。

所以回调只做一件事:把 `pending` 改成 `approved`。执行由这个 job 认领。

## 只做一次靠的是一条 SQL,不是记性

`mark_executed()` 带着 `WHERE status = 'approved'`,所以两个 worker 同时跑时,
第二个在写结果那一刻发现行已经不在 `approved` 了,拿到 None ——
**于是它知道自己白跑了一趟,而不是又发了一条消息**。

顺序上有一个必须注意的地方:**先执行,再改状态**。反过来的话,
改完状态到执行完成之间那一瞬如果进程挂了,那条审批会永远停在 `executed`
而事情根本没做 —— 而 03 的两个零里,"漏做"不在其中,"重复做"在。
两害相权,宁可有极小的概率重发一条,也不要静默地不发。

## 失败了不自动重试

`failed` 不会自己回到 `approved`。要重试得有人再看一眼 ——
因为**失败的原因可能是"对面已经收到了,只是响应超时"**,
而那种情况下重试就是发第二条。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from lifein.alerts import Alerter
from lifein.channels.base import Channel
from lifein.governance.audit import ToolCallRecord
from lifein.governance.registry import ToolContext, ToolLevel, get_tool
from lifein.repos import approvals
from lifein.repos.approvals import Approval
from lifein.repos.tool_calls import record_tool_call

log = logging.getLogger(__name__)

JOB_NAME = "approval_execute"

MAX_PER_RUN = 20
"""一次最多执行几条。**这个数字小是刻意的** —— 一次跑二十条代发消息意味着
出错时也是二十条,而 P3 的验收标准只要求二十次成功的操作,不是二十次并发。"""


@dataclass(frozen=True)
class ExecuteDeps:
    channel: Channel
    alerter: Alerter
    now: Callable[[], datetime]


@dataclass
class ExecuteResult:
    considered: int = 0
    executed: int = 0
    failed: int = 0
    skipped: int = 0
    """被别人抢先做掉了。**不是错误** —— 是"只做一次"在起作用。"""

    expired: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_stats(self) -> dict:
        return {
            "considered": self.considered,
            "executed": self.executed,
            "failed": self.failed,
            "skipped": self.skipped,
            "expired": self.expired,
        }


def run_once(
    user_id: str, session: Session, *, deps: ExecuteDeps, limit: int = MAX_PER_RUN
) -> ExecuteResult:
    """把点过同意的那些做掉。**不认领窗口** —— 它处理的是"状态是 approved 的",
    而那个集合和时间窗没有关系(和对账 job 同一个道理)。
    """
    result = ExecuteResult()
    now = deps.now()

    # 过期清理先做:它和这次有没有东西可执行无关,而一条昨天的审批
    # 留在队列里只会让人某天点下去,发出一条早就不合时宜的消息
    result.expired = approvals.expire_overdue(user_id, session, now=now)

    ready = approvals.list_ready(user_id, session, limit=limit)
    result.considered = len(ready)

    for item in ready:
        _execute_one(user_id, session, item, deps=deps, result=result)
    return result


def _execute_one(
    user_id: str,
    session: Session,
    item: Approval,
    *,
    deps: ExecuteDeps,
    result: ExecuteResult,
) -> None:
    try:
        spec = get_tool(item.tool_name)
    except Exception as exc:  # noqa: BLE001
        # 工具被改名或删掉了,而队列里还有引用它的审批。**不能当成执行成功**
        _fail(user_id, session, item, deps=deps, result=result, error=f"工具找不到:{exc}")
        return

    if spec.level is not ToolLevel.L3:
        # 队列里出现非 L3 说明有人绕过了网关。停下来告警,别执行
        _fail(
            user_id, session, item, deps=deps, result=result,
            error=f"{item.tool_name} 不是 L3,不该出现在审批队列里",
        )
        return

    try:
        args = spec.args_model.model_validate(item.tool_args)
    except Exception as exc:  # noqa: BLE001
        # 入参 schema 改过了,而队列里存的是旧形状。**宁可不做**
        _fail(user_id, session, item, deps=deps, result=result, error=f"入参对不上:{exc}")
        return

    ctx = ToolContext(user_id=user_id, session=session, channel=deps.channel)
    try:
        # **先执行,再改状态**(见模块开头那段)
        value = spec.func(args, ctx)
    except Exception as exc:  # noqa: BLE001
        _fail(
            user_id, session, item, deps=deps, result=result,
            error=f"{type(exc).__name__}: {exc}",
        )
        return

    after = approvals.mark_executed(
        user_id,
        session,
        approval_id=item.id,
        now=deps.now(),
        result=value if isinstance(value, dict) else {"value": str(value)},
    )
    if after is None:
        # 别人抢先做掉了。**这条要说出来** —— 它意味着这次真的发了两遍,
        # 而"零重复执行"是 P3 的退出条件
        message = f"审批 #{item.id} 执行完才发现已经被做过了 —— 可能重复执行了一次"
        log.error(message)
        result.warnings.append(message)
        deps.alerter.alert("可能的重复执行", message)
        result.skipped += 1
        return

    _audit(user_id, session, item, status="allowed")
    result.executed += 1
    log.info("审批 #%s 执行完成:%s", item.id, item.tool_name)


def _fail(
    user_id: str,
    session: Session,
    item: Approval,
    *,
    deps: ExecuteDeps,
    result: ExecuteResult,
    error: str,
) -> None:
    """标成失败并告警。**不自动重试** —— 见模块开头。"""
    approvals.mark_failed(user_id, session, approval_id=item.id, now=deps.now(), error=error)
    _audit(user_id, session, item, status="error")
    result.failed += 1
    result.warnings.append(f"审批 #{item.id} 执行失败:{error}")
    deps.alerter.alert("审批执行失败", f"#{item.id} {item.preview_text}\n{error}")


def _audit(user_id: str, session: Session, item: Approval, *, status: str) -> None:
    """记进 `tool_calls`。**L3 那条记录是"这件事到底谁批准的"唯一的答案。**

    `rollback_info` 里放的是审批 id 而不是"怎么撤" —— 因为**发出去的消息
    撤不回来**(见 `tools/message.py`)。能回答的是"这次是哪一条审批放行的",
    而那正是出事之后要查的第一件事。
    """
    record_tool_call(
        user_id,
        session,
        ToolCallRecord(
            user_id=user_id,
            agent=item.agent,
            tool_name=item.tool_name,
            level=ToolLevel.L3,
            args_digest={"approval_id": {"type": "int", "value": item.id}},
            llm_fields_sent=[],
            result_status=status,
            rollback_info={
                "approval_id": item.id,
                "preview_text": item.preview_text,
                "note": "L3 不可回滚:消息已经发出去了。这条记录回答的是谁批准的",
            },
        ),
    )
