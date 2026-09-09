"""审批执行 job(P3 第 7 片)。**点过同意的那些,在这里真的发生。**

    approvals(approved) → 认领(executing) → 执行工具 → executed / failed

## 为什么执行不在回调里

企微的回调有超时,而"代发消息"要等对面的接口几秒钟。回调里做的话,
**超时重投会再执行一次** —— 而 03 那条"零重复执行"就是这么破的,
而且破得很安静:对面收到两条一样的消息,你这边的日志看起来一切正常。

所以回调只做一件事:把 `pending` 改成 `approved`。执行由这个 job 认领。

## 只做一次靠的是一条 SQL,而且必须在发出去之前

**认领 → 执行 → 记结果**,三步。认领是一条带条件的 UPDATE
(`approved` → `executing`),第二个执行者拿到 0 行,
**在发出去之前**就知道自己白跑了。

原来这里是两步:先执行,再让 `mark_executed()` 带着 `WHERE status='approved'`
去写结果。当时的理由写着"两害相权,宁可有极小的概率重发一条,也不要静默地
不发"。**那个取舍算错了两件事:**

1. **概率不小。** `list_ready` 取的是 `approved` 的行,两个执行者会同时取到
   同一条、同时执行、同时发出去,只是其中一个在写结果时才发现自己是第二个 ——
   而那时消息已经出去了。"极小的概率"描述的是崩溃窗口,而这里的窗口是
   **整个执行时长**,代发一条消息要等对面几秒钟
2. **代价的方向反了。** 03 的 P3 退出条件写着"出现任何一次重复执行 →
   停止 L3 上线"。而"漏做"不在退出条件里。拿一个会触发退出条件的风险,
   去换一个不在退出条件里的风险

## 卡在 executing 的那些:告警,不重试

新顺序的代价是:认领之后、拿到结果之前进程挂了,那条会停在 `executing`。
它的含义是"**我们开始发了,但不知道发出去没有**" —— 而那是唯一不能替用户
猜的情况:重试可能发第二条,标成失败会让人以为一条都没发。

所以这个 job 每次跑都数一遍卡住的,超过 `STUCK_AFTER` 就告警。
`admin approvals` 里看得到具体是哪一条。

## 失败了不自动重试

`failed` 不会自己回到 `approved`。要重试得有人再看一眼 ——
因为**失败的原因可能是"对面已经收到了,只是响应超时"**,
而那种情况下重试就是发第二条。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from lifein.alerts import Alerter
from lifein.channels.base import Channel
from lifein.governance.audit import ToolCallRecord
from lifein.governance.registry import ToolContext, ToolLevel, executing, get_tool
from lifein.repos import approvals
from lifein.repos.approvals import Approval
from lifein.repos.tool_calls import record_tool_call

log = logging.getLogger(__name__)

JOB_NAME = "approval_execute"

MAX_PER_RUN = 20
"""一次最多执行几条。**这个数字小是刻意的** —— 一次跑二十条代发消息意味着
出错时也是二十条,而 P3 的验收标准只要求二十次成功的操作,不是二十次并发。"""

STUCK_AFTER = timedelta(minutes=10)
"""认领之后多久还没结果,就算卡住了。

代发一条消息是秒级的事,十分钟只可能是进程在中途没了。给得比"秒级"宽得多,
是因为**误报的代价比迟报大**:一条其实正常的执行被报成"可能发出去了",
会让人去查一件没发生的事,而下一次真的卡住时他不会再当回事。
"""


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
    stuck: int = 0
    """卡在 `executing` 的条数。**这个数不该大于 0** ——
    大于 0 意味着有一条消息"可能发出去了,也可能没有",要人去看。"""

    warnings: list[str] = field(default_factory=list)

    def as_stats(self) -> dict:
        return {
            "considered": self.considered,
            "executed": self.executed,
            "failed": self.failed,
            "skipped": self.skipped,
            "expired": self.expired,
            "stuck": self.stuck,
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

    _report_stuck(user_id, session, deps=deps, now=now, result=result)

    ready = approvals.list_ready(user_id, session, limit=limit)
    result.considered = len(ready)

    for item in ready:
        _execute_one(user_id, session, item, deps=deps, result=result)
    return result


def _report_stuck(
    user_id: str,
    session: Session,
    *,
    deps: ExecuteDeps,
    now: datetime,
    result: ExecuteResult,
) -> None:
    """数一遍卡在 `executing` 的。**只报,不动它们。**

    重试可能发第二条,标成失败会让人以为一条都没发 —— 两个都是替用户
    做了一个只有他能做的判断。这里唯一该做的是让他看见。
    """
    stuck = approvals.list_stuck(user_id, session, cutoff=now - STUCK_AFTER)
    result.stuck = len(stuck)
    if not stuck:
        return

    ids = ", ".join(f"#{item.id}" for item in stuck)
    message = (
        f"{len(stuck)} 条审批卡在执行中({ids}):开始发了但没拿到结果。"
        "**可能已经发出去了** —— 用 admin approvals 看一眼,"
        "确认之后手动改状态,不要直接重跑"
    )
    log.error(message)
    result.warnings.append(message)
    deps.alerter.alert("审批卡在执行中", message)


def _execute_one(
    user_id: str,
    session: Session,
    item: Approval,
    *,
    deps: ExecuteDeps,
    result: ExecuteResult,
) -> None:
    # **认领在最前面,在读工具之前。** 后面每一条失败路径都要写状态,
    # 而写状态的起点是 `executing` —— 没认领就写不动,那条会留在 `approved`
    # 里,下一轮再失败一遍、再发一封告警
    claimed = approvals.claim_for_execution(
        user_id, session, approval_id=item.id, now=deps.now()
    )
    if claimed is None:
        # 别人抢先认领了。**这是"只做一次"在起作用,不是错误** ——
        # 而且这一次什么都没发出去,和原来那条"执行完才发现"完全不同
        result.skipped += 1
        return

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
        # 这一步会动外部世界。**到这里为止这条已经是 `executing` 了** ——
        # 别的执行者认领不到,所以不会有第二条消息发出去
        # **这是铁律 4 唯一的合法例外**,所以要显式写出来(见 `registry.executing`)。
        # 这次调用在**进审批队列那一刻**已经过了网关:等级、白名单、入参、
        # trust 全查过了。人点同意之后再过一次网关,只会把它重新变成一条审批
        with executing(spec.name):
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
        # 认领过了却写不回去。**理论上进不来** —— 认领到 `executing` 的只有
        # 我们自己,而状态机里没有别的路径能把它从 `executing` 挪走。
        # 真出现说明有人手动改了库,或者有第二处代码在动这张表
        message = (
            f"审批 #{item.id} 执行完写不回结果 —— 状态被别处改过了。"
            "**这条消息可能已经发出去了**"
        )
        log.error(message)
        result.warnings.append(message)
        deps.alerter.alert("审批状态被别处改过", message)
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
