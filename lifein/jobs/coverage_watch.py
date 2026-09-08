"""覆盖率巡检(P2 第 11 片)—— [R8](../../docs/05-risks.md#r8--数据源格式变动) 的早期信号。

R8 那条写着一句要紧的话:

> **覆盖率指标是这条风险的早期信号** —— 某个来源的通知突然解析不出来,
> 会先表现为覆盖率下降,而不是报错。定期看覆盖率,不要只看错误日志。

这个 job 就是"定期看"。它**不修任何东西,也不推给用户** —— 它只在数字开始
变坏的时候告诉你,而那正是 R8 唯一能被及时发现的方式。

## 四个数,各盯一种失效

| 数 | 变坏的意思 |
| --- | --- |
| 实时通道覆盖率 | 手机端漏采,或者通知文案改了解析不出来 |
| 归一化失败条数 | 采集到了但解不开 —— 格式变动最直接的表现 |
| 模型归类占比 | 规则表没有正确沉淀(ADR-008 说这就是 bug) |
| 待确认积压 | 判据太保守,或者用户已经不看那个队列了 |

**四个都不是"错误",都是趋势。** 所以这个 job 报的是数字和阈值,
不是异常 —— 它每天说一次话,而不是等到出事才说。

## 为什么不做成推送给用户

这些数字是运维信息,用户看不懂也帮不上忙。R4 那条"误报两次就足够让人
永久关掉通知"在这里的形态是:**给用户推一条他做不了任何事的消息,
下一次真正要紧的推送就少一个人看**。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifein.alerts import Alerter
from lifein.repos import pending, raw_events

log = logging.getLogger(__name__)

JOB_NAME = "coverage_watch"

WINDOW = timedelta(days=30)
"""看多长一段。一个月:短了会被"这周出差没消费"带偏,
长了则要等好几周才看得出格式变动 —— 而 R8 要的是**早期**信号。"""

MIN_TRANSACTIONS = 20
"""少于这么多笔就不判断覆盖率。**样本太小时那个比例没有意义** ——
三笔里有一笔没对上就是 67%,而它什么都说明不了,只会每天来一条假告警。"""

COVERAGE_FLOOR = 0.8
"""03 的验收标准:实时通道覆盖率 > 80%。低于它就说话。"""

FAILED_PARSE_CEILING = 5
"""一个月里归一化失败多少条算不正常。**这个数要小** ——
解析失败是 R8 最直接的表现,而它从来不会自己变好。"""

LLM_SHARE_CEILING = 0.5
"""模型归类占比的上限。ADR-008 说这个数不随时间下降就是 bug ——
这里只能看某一时刻的值,趋势要人去翻历史。"""

BACKLOG_CEILING = 30


@dataclass(frozen=True)
class CoverageDeps:
    alerter: Alerter


@dataclass
class CoverageReport:
    window_start: datetime
    window_end: datetime
    transactions: int = 0
    reconciled: int = 0
    failed_parses: int = 0
    by_rule: int = 0
    by_llm: int = 0
    pending_backlog: int = 0
    concerns: list[str] = field(default_factory=list)
    """具体哪几条不对劲。**空的才是好消息**,而且这个列表就是告警正文。"""

    @property
    def coverage(self) -> float | None:
        """实时通道覆盖率。**样本太小时是 None,不是某个数字** ——
        三笔里对上两笔算不出任何结论。"""
        if self.transactions < MIN_TRANSACTIONS:
            return None
        return self.reconciled / self.transactions

    @property
    def llm_share(self) -> float | None:
        from lifein.repos.merchant_rules import llm_share

        return llm_share({"by_rule": self.by_rule, "by_llm": self.by_llm})

    @property
    def healthy(self) -> bool:
        return not self.concerns

    def as_stats(self) -> dict:
        return {
            "transactions": self.transactions,
            "reconciled": self.reconciled,
            "coverage": self.coverage,
            "failed_parses": self.failed_parses,
            "llm_share": self.llm_share,
            "pending_backlog": self.pending_backlog,
            "concerns": len(self.concerns),
        }


_TRANSACTIONS = text("""
    SELECT
        count(*) AS total,
        count(*) FILTER (WHERE stage = 'reconciled') AS reconciled
      FROM transactions
     WHERE user_id = :user_id
       AND occurred_at >= :start AND occurred_at < :end
""")

_CATEGORIZED = text("""
    -- 归类是靠规则还是靠模型,**记账 job 每一轮都已经数好了**,就在 job_runs.stats 里。
    -- 从 transactions 反推是推不出来的:一条归好类的记录上看不出当初是谁归的,
    -- 而估出来的占比会在规则表刚建起来那阵子系统性地偏高
    SELECT
        COALESCE(sum((stats ->> 'categorized_by_rule')::int), 0) AS by_rule,
        COALESCE(sum((stats ->> 'categorized_by_llm')::int), 0) AS by_llm
      FROM job_runs
     WHERE user_id = :user_id
       -- 只有记账 job 记这两个数。对账 job 不认领窗口,也就不写 job_runs
       AND job_name = 'bookkeeping'
       AND status = 'succeeded'
       AND window_start >= :start
""")


def run_once(
    user_id: str,
    session: Session,
    *,
    deps: CoverageDeps,
    now: datetime,
    window: timedelta = WINDOW,
) -> CoverageReport:
    """看一遍这几个数,不对劲就告警。**不认领窗口** ——
    每天看同一段最近三十天是有意义的,而"补看"上个月的覆盖率没有意义。
    """
    start = now - window
    report = CoverageReport(window_start=start, window_end=now)

    counts = session.execute(
        _TRANSACTIONS, {"user_id": user_id, "start": start, "end": now}
    ).one()
    report.transactions = counts.total
    report.reconciled = counts.reconciled

    categorized = session.execute(_CATEGORIZED, {"user_id": user_id, "start": start}).one()
    report.by_rule = int(categorized.by_rule)
    report.by_llm = int(categorized.by_llm)

    report.failed_parses = raw_events.count_failed(user_id, session, since=start)
    report.pending_backlog = pending.count_pending(user_id, session, now=now)

    _judge(report)
    if report.concerns:
        deps.alerter.alert(
            "账本的几个数字不太对",
            "\n".join(report.concerns) + "\n\n(这是趋势提醒,不是错误。见 R8)",
        )
    return report


def _judge(report: CoverageReport) -> None:
    """把数字变成人话。**每一条都要说清"这意味着什么"** ——
    一条只有数字的告警,读的人还得自己回忆阈值是多少、为什么定在那里。
    """
    coverage = report.coverage
    if coverage is not None and coverage < COVERAGE_FLOOR:
        report.concerns.append(
            f"实时通道覆盖率 {coverage:.0%}(低于 {COVERAGE_FLOOR:.0%}):"
            "对账单里有些笔实时通道没采到,可能是通知文案改了,或者手机端掉线过"
        )

    if report.failed_parses > FAILED_PARSE_CEILING:
        report.concerns.append(
            f"最近有 {report.failed_parses} 条事件归一化失败:"
            "这是数据源格式变动最直接的表现,原文都还在,解析器改好能重跑"
        )

    share = report.llm_share
    if share is not None and share > LLM_SHARE_CEILING:
        report.concerns.append(
            f"归类里有 {share:.0%} 靠模型:规则表可能没有正确沉淀,"
            "而 ADR-008 说这个数不随时间下降就是 bug,不是常态"
        )

    if report.pending_backlog > BACKLOG_CEILING:
        report.concerns.append(
            f"待确认攒了 {report.pending_backlog} 条:"
            "要么判据太保守,要么那个队列已经没人看了 —— 两种都要处理"
        )
