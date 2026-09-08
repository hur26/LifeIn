"""预算预警规则(P2 第 9 片)。

03 的验收标准是**超支当天发出,不是月底**。所以它是一条规则而不是一个月度
任务:提醒 job 每十几分钟扫一次,超支那一刻的下一次扫描就会说话。

## 一条规则,两条线

`alert_threshold`(默认 0.9)和 100% 各发一次,`dedup_key` 里带着级别 ——
**它们是两件不同的事**:

- **接近**:还来得及改变行为。价值全在"提前"上
- **超了**:已经发生了。价值在"你知道了"

共用一个 key 的话,90% 那天发过之后,真正超支那天会被冷却期挡掉 ——
而那一天恰恰是最该说话的。

## 冷却期是一个月

别的规则默认一天,这条不行:一条预算超了之后**每天都还是超着**,
一天一条的话月底会连着提十几次同一件事,而 R4 说误报两次就足够让人
永久关掉通知。`dedup_key` 里带着账单周期,所以下个月会重新开始。

## 只在有预算时说话

没设预算就一条都不发。**不给一个"默认预算"** —— 猜出来的额度一定是错的,
而一条错的预算发出的每一次提醒都是误报。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

from lifein.channels.base import Card, CardSection
from lifein.repos import budgets
from lifein.rules.base import Reminder, Rule, RuleContext

log = logging.getLogger(__name__)

COOLDOWN = timedelta(days=31)
"""同一条预算的同一个级别,一个周期内只说一次。见模块开头。"""

NEAR = "near"
OVER = "over"


def _budget_alerts(ctx: RuleContext) -> Sequence[Reminder]:
    reminders: list[Reminder] = []
    for item in budgets.progress(ctx.user_id, ctx.session, now=ctx.now):
        level = OVER if item.over else (NEAR if item.near else None)
        if level is None:
            continue
        reminders.append(_reminder(item, level=level))
    return reminders


def _reminder(item: budgets.BudgetProgress, *, level: str) -> Reminder:
    name = item.budget.category or "总预算"
    period = item.period_start.strftime("%Y-%m")
    percent = int(item.ratio * 100)

    if level == OVER:
        card = Card(
            title=f"{name}超了",
            summary=f"{period} 已花 {_yuan(item.spent)},预算 {_yuan(item.budget.amount)}",
            sections=[
                CardSection(
                    heading="超出",
                    # 说"超了多少"而不是"花了百分之多少":超支之后人想知道的是
                    # 要补多少,而百分比在这个时候只是一个更难算的说法
                    lines=[f"{_yuan(-item.remaining)}({percent}%)"],
                )
            ],
            footer="按已入账的支出算,还款和退款不计入",
        )
    else:
        card = Card(
            title=f"{name}快到了",
            summary=f"{period} 已花 {_yuan(item.spent)},还剩 {_yuan(item.remaining)}",
            sections=[CardSection(heading="进度", lines=[f"{percent}%"])],
            footer="按已入账的支出算,还款和退款不计入",
        )

    return Reminder(
        card=card,
        # 周期在 key 里:下个月是新的一条,不会被这个月的冷却期挡住。
        # 级别也在:90% 说过之后,真正超支那天还要能说第二次
        dedup_key=f"budget:{item.budget.category or 'total'}:{period}:{level}",
        cooldown=COOLDOWN,
    )


def _yuan(amount: Decimal) -> str:
    return f"{amount:.2f} 元"


BUDGET_ALERT = Rule(
    rule_id="budget_alert",
    description="预算用到阈值时提醒一次,超支当天再提醒一次,每个周期各一次",
    evaluate=_budget_alerts,
)
