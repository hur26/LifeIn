"""预算(06 的 `budgets`)。P2 第 9 片。

03 给这一片的验收标准只有一句,但它决定了整片的形状:

> 预算超支预警在**超支当天**发出,不是月底

所以预算不是一张"月底拿来对账的表",而是**每次扫描都要重新算一遍的东西**。
算的是"这个月到现在花了多少",而不是"上个月花了多少" ——
后者算得再准也帮不上任何忙。

## 两条线,不是一条

`alert_threshold`(默认 0.9)和 100% 是两件不同的事:

- **接近**:还来得及改变行为。这一条的价值全在"提前"上
- **超了**:已经发生了。这一条的价值在"你知道了",而不在"你还能做什么"

合成一条的话只能二选一:卡在 90% 会让真正超支那天反而没有消息(冷却期还没过),
卡在 100% 则永远没有提前量。

## 只算支出

`kind = 'expense'` —— 还款、转账、退款都不进。理由和 `TxnKind.counts_as_spending`
一样:信用卡还款计入支出就是双重记账,而退款是钱回来。

**退款不冲减预算**,这是一个刻意的选择:退款可能发生在下个月,冲减会让
"八月花了多少"这个数在九月还在变,而一个会变的历史数字没法用来判断
"这个月是不是花超了"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifein.repos.transactions import CATEGORIES

log = logging.getLogger(__name__)

MONTH = "month"
"""目前只支持按月。周预算、年预算的取值留给以后 —— 库上是 TEXT,加不用迁移。"""

TOTAL = None
"""总预算的 `category`。**NULL 表示"不分类目的总额"**,不是"没设分类"。"""

DEFAULT_THRESHOLD = Decimal("0.9")


@dataclass(frozen=True)
class Budget:
    id: int
    category: str | None
    period: str
    amount: Decimal
    alert_threshold: Decimal

    @property
    def is_total(self) -> bool:
        return self.category is None


@dataclass(frozen=True)
class BudgetProgress:
    """一条预算在当期的进度。**`spent` 是"到现在为止",不是"整个月"。**"""

    budget: Budget
    spent: Decimal
    period_start: datetime
    period_end: datetime

    @property
    def ratio(self) -> Decimal:
        if self.budget.amount <= 0:
            return Decimal(0)
        return self.spent / self.budget.amount

    @property
    def over(self) -> bool:
        return self.spent > self.budget.amount

    @property
    def near(self) -> bool:
        """到了阈值但还没超。**超了就不算"接近"** —— 两条线是两件事,
        一笔消费同时触发两条提醒只会让人觉得系统在重复说话。"""
        return not self.over and self.ratio >= self.budget.alert_threshold

    @property
    def remaining(self) -> Decimal:
        return self.budget.amount - self.spent


class BudgetError(ValueError):
    """预算设得不对。都在写库之前抛,带得出人话的原因。"""


_COLUMNS = "id, category, period, amount, alert_threshold"

_UPSERT = text(f"""
    INSERT INTO budgets (user_id, category, period, amount, alert_threshold)
    VALUES (:user_id, :category, :period, :amount, :alert_threshold)
    -- 总预算的 category 是 NULL,而 Postgres 默认把 NULL 之间看成互不相同。
    -- 靠迁移 0009 那条 NULLS NOT DISTINCT 的约束,这里才撞得上
    ON CONFLICT (user_id, category, period)
    DO UPDATE SET amount = EXCLUDED.amount, alert_threshold = EXCLUDED.alert_threshold
    RETURNING {_COLUMNS}
""")

_LIST = text(f"""
    SELECT {_COLUMNS} FROM budgets
     WHERE user_id = :user_id AND period = :period
     -- 总预算(category IS NULL)排最前:它是"这个月总共能花多少",
     -- 是看这张表时第一个想知道的数
     ORDER BY category NULLS FIRST
""")

_DELETE = text("""
    DELETE FROM budgets
     WHERE user_id = :user_id AND period = :period
       AND category IS NOT DISTINCT FROM CAST(:category AS TEXT)
""")

_SPENT_BY_CATEGORY = text("""
    SELECT COALESCE(category, '') AS category, sum(amount) AS total
      FROM transactions
     WHERE user_id = :user_id
       AND kind = 'expense'
       AND occurred_at >= :start AND occurred_at < :end
     GROUP BY 1
""")

_SPENT_TOTAL = text("""
    SELECT COALESCE(sum(amount), 0) AS total
      FROM transactions
     WHERE user_id = :user_id
       AND kind = 'expense'
       AND occurred_at >= :start AND occurred_at < :end
""")


def set_budget(
    user_id: str,
    session: Session,
    *,
    amount: Decimal,
    category: str | None = TOTAL,
    period: str = MONTH,
    alert_threshold: Decimal = DEFAULT_THRESHOLD,
) -> Budget:
    """设一条预算。同一个(类目, 周期)重复设就是改额度。

    **分类必须在枚举内**,和记账 agent 第 4 层同一个枚举 —— 给一个枚举外的
    类目设预算,那条预算永远不会有任何一笔消费落进来,而它看起来在正常工作。
    """
    if amount <= 0:
        raise BudgetError(f"预算必须是正数:{amount}")
    if not Decimal("0.1") <= alert_threshold <= Decimal("1"):
        # 低于 0.1 等于每个月一开始就在报警,高于 1 等于这条线在超支之后 ——
        # 两种都让"接近"这条线失去意义
        raise BudgetError(f"预警阈值要在 0.1 和 1 之间:{alert_threshold}")
    if category is not None and category not in CATEGORIES:
        raise BudgetError(f"分类不在枚举内:{category}")

    row = session.execute(
        _UPSERT,
        {
            "user_id": user_id,
            "category": category,
            "period": period,
            "amount": amount,
            "alert_threshold": alert_threshold,
        },
    ).one()
    return _to_budget(row)


def list_budgets(user_id: str, session: Session, *, period: str = MONTH) -> list[Budget]:
    rows = session.execute(_LIST, {"user_id": user_id, "period": period}).all()
    return [_to_budget(row) for row in rows]


def delete_budget(
    user_id: str, session: Session, *, category: str | None, period: str = MONTH
) -> bool:
    """删一条预算。**删了就不再预警**,而不是把额度设成很大 ——
    后者会在报表上留下一条永远用不满的假预算。"""
    return session.execute(
        _DELETE, {"user_id": user_id, "category": category, "period": period}
    ).rowcount > 0


def progress(
    user_id: str, session: Session, *, now: datetime, period: str = MONTH
) -> list[BudgetProgress]:
    """算每条预算当期花到哪儿了。**每次扫描都重新算**(见模块开头)。

    一次查库拿到所有类目的合计,再在内存里分配到各条预算上 ——
    预算最多十来条,而每条查一次库会让这条规则在每次扫描时打十几个来回。
    """
    budgets = list_budgets(user_id, session, period=period)
    if not budgets:
        return []

    start, end = period_bounds(now, period=period)
    by_category = {
        row.category: Decimal(row.total)
        for row in session.execute(
            _SPENT_BY_CATEGORY, {"user_id": user_id, "start": start, "end": end}
        ).all()
    }
    total = Decimal(
        session.execute(
            _SPENT_TOTAL, {"user_id": user_id, "start": start, "end": end}
        ).scalar_one()
    )

    return [
        BudgetProgress(
            budget=budget,
            # 总预算用总额,不是各类目之和:**没归类的那些也是花掉的钱**,
            # 漏掉它们会让总预算永远看起来还有富余
            spent=total if budget.is_total else by_category.get(budget.category, Decimal(0)),
            period_start=start,
            period_end=end,
        )
        for budget in budgets
    ]


def period_bounds(now: datetime, *, period: str = MONTH) -> tuple[datetime, datetime]:
    """当期的起止。**用 `now` 的时区**,不是 UTC。

    月度预算按本地月份切:一笔 8 月 31 日晚上的消费在 UTC 已经是 9 月 1 日,
    算进九月会让八月的数字少一笔、九月多一笔,而两个月的判断都因此错了。
    """
    if period != MONTH:
        raise BudgetError(f"暂时只支持按月的预算:{period}")

    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = _first_of_next_month(start.date())
    end = start.replace(year=next_month.year, month=next_month.month, day=1)
    return start, end


def _first_of_next_month(day: date) -> date:
    return date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)


def _to_budget(row) -> Budget:
    return Budget(
        id=row.id,
        category=row.category,
        period=row.period,
        amount=Decimal(row.amount),
        alert_threshold=Decimal(row.alert_threshold),
    )
