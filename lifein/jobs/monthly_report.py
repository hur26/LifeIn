"""月度报告 job(P2 第 10 片)。

    上个月的交易 → SQL 算出统计 → agent 写两句观察 → 卡片推给你

**它在月初跑,算的是上个月。** 不是"最近 30 天":一份跨月的报告没法回答
"这个月花超了没有",而那正是看月报的人想知道的事。

## 一个月只发一次,靠 `job_runs` 而不是靠日期判断

别的 job 都认领窗口,这个也认 —— 窗口就是上个月那整整一个月。
判断"今天是不是一号"会在两种情况下出错:进程在一号那天没起来(补不回来),
以及一号那天重启了两次(发两遍)。而**一份月报发两遍比不发更让人烦**,
因为第二遍的内容和第一遍一模一样,收到的人会以为系统坏了。

## 报告走推送,不走告警

告警通道是给运维看的("采集器掉线了"),月报是给用户看的。
混在一起的后果是:某天你为了少收告警把那个通道关小声了,
月报跟着一起没了(R4 那条"误报两次就足够让人永久关掉通知"的变体)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from lifein.agents.monthly_report import (
    MonthlyInput,
    MonthlyReportFailed,
    to_card,
    to_long_card,
    write_notes,
)
from lifein.alerts import Alerter
from lifein.channels.base import Channel
from lifein.governance.audit import ToolCallRecord
from lifein.governance.registry import ToolLevel
from lifein.llm.client import LLMClient
from lifein.repos import budgets, job_runs, push_log, reports
from lifein.repos.tool_calls import record_tool_call

log = logging.getLogger(__name__)

JOB_NAME = "monthly_report"
AGENT = "monthly_report"


@dataclass(frozen=True)
class MonthlyDeps:
    llm: LLMClient
    channel: Channel
    alerter: Alerter
    email: Channel | None = None
    """邮件长版的出口(03 那句"企微卡片 + 邮件长版")。

    **没配邮件时是 None,那时只有卡片那一份** —— 而卡片会被通道的长度上限截断
    (企微 2048 字节、微信 4000 字),截断之后**看起来仍然是一份完整的报告**,
    只是后面几类没了。所以没配邮件是一个要知道的缺口,不是可有可无的增强。
    """


@dataclass
class MonthlyJobResult:
    period: str = ""
    skipped: bool = False
    no_transactions: bool = False
    notes: int = 0
    notes_text: list[str] = field(default_factory=list)
    """评语原文。**存进 `job_runs.stats`,App 的月度报表直接读它** ——
    手机上点开就现调一次模型既慢又贵,而同一个月的评语每次点开都不一样,
    会让人以为数字也在变(06 §6.11)。"""

    dropped_invented: int = 0
    delivered: bool = False
    long_version_sent: bool = False
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_stats(self) -> dict:
        return {
            "period": self.period,
            "notes": self.notes,
            "notes_text": list(self.notes_text),
            "dropped_invented": self.dropped_invented,
            "delivered": self.delivered,
            "long_version_sent": self.long_version_sent,
            "no_transactions": self.no_transactions,
        }


def run_once(
    user_id: str, session: Session, *, deps: MonthlyDeps, now: datetime
) -> MonthlyJobResult:
    """把上个月的报告发出去。**同一个月只会发一次。**"""
    last_month = _a_day_in_last_month(now)
    start, end = budgets.period_bounds(last_month)
    result = MonthlyJobResult(period=start.strftime("%Y-%m"))

    if not job_runs.claim_window(
        user_id, session, job_name=JOB_NAME, window_start=start, window_end=end
    ):
        # 已经发过了。**这是常态而不是异常** —— 进程一天重启五次也只发一份
        result.skipped = True
        return result

    try:
        _send(user_id, session, deps=deps, now=last_month, result=result)
    except MonthlyReportFailed as exc:
        result.error = str(exc)
        deps.alerter.alert("月度报告生成失败", str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("月度报告 job 异常")
        result.error = f"{type(exc).__name__}: {exc}"
        deps.alerter.alert("月度报告异常终止", result.error)

    job_runs.finish_window(
        user_id,
        session,
        job_name=JOB_NAME,
        window_start=start,
        status="failed" if result.error else "succeeded",
        stats=result.as_stats(),
        error=result.error,
    )
    return result


def _send(
    user_id: str,
    session: Session,
    *,
    deps: MonthlyDeps,
    now: datetime,
    result: MonthlyJobResult,
) -> None:
    report = reports.monthly(user_id, session, now=now)
    if report.is_empty:
        # 一笔都没有的月份不发报告。**空报告比不发更伤信任** ——
        # 它会让人以为记账在正常工作,而实际上一条都没采到
        log.info("%s 一笔交易都没有,不发月度报告", report.period)
        result.no_transactions = True
        return

    written = write_notes(MonthlyInput(report=report), llm=deps.llm)
    result.notes = len(written.output.notes)
    result.notes_text = list(written.output.notes)
    result.dropped_invented = written.output.dropped_invented

    record_tool_call(
        user_id,
        session,
        ToolCallRecord(
            user_id=user_id,
            agent=AGENT,
            tool_name="llm.chat",
            level=ToolLevel.L1,
            # 送出去的是算好的统计,不是原始交易 —— 审计里要看得出这一点(R12)
            args_digest={"period": {"type": "str", "value": report.period}},
            llm_fields_sent=written.llm_fields_sent,
            result_status="allowed",
            prompt_tokens=written.prompt_tokens,
            completion_tokens=written.completion_tokens,
        ),
    )

    if written.output.dropped_invented:
        # **不该长期大于 0**:大了说明 prompt 在诱导模型算账,而它不该算账
        message = f"{report.period} 的月报里有 {written.output.dropped_invented} 条编了数字"
        result.warnings.append(message)
        log.warning(message)

    card = to_card(report, written.output)
    try:
        delivery = deps.channel.send(user_id, card)
    except Exception as exc:  # noqa: BLE001
        # 推失败也要写 push_log:"发过但没送到"是排查通道问题时唯一的线索
        push_log.record_push(
            user_id,
            session,
            channel=deps.channel.name,
            mode="active",
            card=card,
            delivered=False,
            error=f"{type(exc).__name__}: {exc}",
        )
        deps.alerter.alert("月度报告推送失败", str(exc))
        result.error = f"推送失败:{exc}"
        return

    push_log.record_push(
        user_id, session, channel=delivery.channel, mode="active", card=card, delivered=True
    )
    result.delivered = True
    _send_long_version(user_id, session, deps=deps, report=report, output=written.output,
                       result=result)



def _send_long_version(
    user_id: str,
    session: Session,
    *,
    deps: MonthlyDeps,
    report,
    output,
    result: MonthlyJobResult,
) -> None:
    """再发一封完整的。**失败不算这次 job 失败** —— 卡片已经送到了,
    而长版是锦上添花的那一份;为它把整个月报标成失败,下个月会重发一遍。
    """
    if deps.email is None or deps.email.name == deps.channel.name:
        # 没配邮件,或者主通道本来就是邮件(那时卡片那份已经是全的)
        return

    card = to_long_card(report, output)
    try:
        delivery = deps.email.send(user_id, card)
    except Exception as exc:  # noqa: BLE001
        log.warning("月报长版没发出去:%s", exc)
        result.warnings.append(f"长版邮件没发出去:{exc}")
        push_log.record_push(
            user_id, session, channel=deps.email.name, mode="active", card=card,
            delivered=False, error=f"{type(exc).__name__}: {exc}",
        )
        return

    push_log.record_push(
        user_id, session, channel=delivery.channel, mode="active", card=card, delivered=True
    )
    result.long_version_sent = True


def _a_day_in_last_month(now: datetime) -> datetime:
    """上个月的某一天。**从这个月一号往回退一天。**

    不减 30 天:三月一日减 30 天会退到一月,于是三月发的是一月的报告 ——
    而那份报告看起来完全正常,只是月份写着一月。
    """
    first_of_this_month = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return first_of_this_month - timedelta(days=1)
