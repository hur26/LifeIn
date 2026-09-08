"""月度账本统计(P2 第 10 片的数字那一半)。

**这个模块里没有模型。** 报告的每一个数字都来自 SQL,而
[铁律 9](../../AGENTS.md#1-铁律) 说能用规则拿到的字段不许交给 LLM ——
这里是那条铁律最直白的落点:一份月度报告全是钱的数字,让模型去算它们
既贵又会算错,而**算错的月度报告和算对的长得一模一样**。

模型在第 10 片里只做一件事:看着这些算好的数字写两句人话(见
`agents/monthly_report.py`)。数字进模型,是为了让它有话可说;
数字出模型,一个都不采信。

## 和预算那一片共用同一个"什么算支出"

`kind = 'expense'`,还款、转账、退款都不进 —— 和 `repos/budgets.py` 一致。
两处口径不一样的话,报告上写着"这个月花了 3000",预算却说"超了 500/2000",
而用户没有办法知道该信哪个。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifein.repos.budgets import period_bounds

log = logging.getLogger(__name__)

TOP_MERCHANTS = 5
"""商户榜列几个。**五个是"看得完"和"看得出问题"之间的折中** ——
十个会让人跳过整段,三个则常常全是外卖平台,看不出结构。"""


@dataclass(frozen=True)
class CategoryLine:
    category: str
    total: Decimal
    count: int
    last_total: Decimal | None = None
    """上个月同一类目花了多少。**None 表示上个月这一类没有记录**,
    不是 0 —— "上个月没花"和"上个月没记"是两件事,而第一个月全是后者。"""

    @property
    def delta(self) -> Decimal | None:
        return None if self.last_total is None else self.total - self.last_total


@dataclass(frozen=True)
class MerchantLine:
    merchant: str
    total: Decimal
    count: int


@dataclass(frozen=True)
class MonthlyReport:
    period: str
    period_start: datetime
    period_end: datetime
    total: Decimal
    count: int
    last_total: Decimal | None
    categories: list[CategoryLine] = field(default_factory=list)
    merchants: list[MerchantLine] = field(default_factory=list)
    uncategorized: Decimal = Decimal(0)
    """没归类的金额。**单独列出来**:它混进"其他"里的话,报告会声称
    自己看懂了这些钱,而实际上没有 —— 这个数大就说明归类那一层要修。"""

    reconciled_ratio: float | None = None
    """这个月有多少笔已经被对账单核对过(03 的实时通道覆盖率)。
    None 表示这个月一笔都没有。"""

    @property
    def delta(self) -> Decimal | None:
        return None if self.last_total is None else self.total - self.last_total

    @property
    def is_empty(self) -> bool:
        return self.count == 0


_TOTALS = text("""
    SELECT COALESCE(sum(amount), 0) AS total, count(*) AS count
      FROM transactions
     WHERE user_id = :user_id AND kind = 'expense'
       AND occurred_at >= :start AND occurred_at < :end
""")

_BY_CATEGORY = text("""
    SELECT COALESCE(category, '') AS category, sum(amount) AS total, count(*) AS count
      FROM transactions
     WHERE user_id = :user_id AND kind = 'expense'
       AND occurred_at >= :start AND occurred_at < :end
     GROUP BY 1
     ORDER BY total DESC
""")

_BY_MERCHANT = text("""
    SELECT merchant_raw, sum(amount) AS total, count(*) AS count
      FROM transactions
     WHERE user_id = :user_id AND kind = 'expense'
       AND occurred_at >= :start AND occurred_at < :end
       AND merchant_raw IS NOT NULL AND merchant_raw <> ''
     GROUP BY 1
     ORDER BY total DESC
     LIMIT :limit
""")

_RECONCILED = text("""
    SELECT
        count(*) FILTER (WHERE stage = 'reconciled') AS reconciled,
        count(*) AS total
      FROM transactions
     WHERE user_id = :user_id
       AND occurred_at >= :start AND occurred_at < :end
""")


def monthly(
    user_id: str, session: Session, *, now: datetime, top_merchants: int = TOP_MERCHANTS
) -> MonthlyReport:
    """算一个月的账。**`now` 落在哪个月就算哪个月。**

    月度报告要在月初发上个月的,所以调用方传的是上个月里的某一天,
    不是"今天减 30 天" —— 后者在三月一日会算到一月去。
    """
    start, end = period_bounds(now)
    last_start, _ = period_bounds(_a_day_in_previous_month(start))

    totals = session.execute(
        _TOTALS, {"user_id": user_id, "start": start, "end": end}
    ).one()
    last = session.execute(
        _TOTALS, {"user_id": user_id, "start": last_start, "end": start}
    ).one()

    categories = _categories(user_id, session, start=start, end=end, last_start=last_start)
    merchants = [
        MerchantLine(merchant=row.merchant_raw, total=Decimal(row.total), count=row.count)
        for row in session.execute(
            _BY_MERCHANT,
            {"user_id": user_id, "start": start, "end": end, "limit": top_merchants},
        ).all()
    ]

    coverage = session.execute(
        _RECONCILED, {"user_id": user_id, "start": start, "end": end}
    ).one()

    return MonthlyReport(
        period=start.strftime("%Y-%m"),
        period_start=start,
        period_end=end,
        total=Decimal(totals.total),
        count=totals.count,
        # 上个月一笔都没有时是 None 而不是 0:第一个月的"环比 -100%"是假的
        last_total=Decimal(last.total) if last.count else None,
        categories=[line for line in categories if line.category],
        merchants=merchants,
        uncategorized=next(
            (line.total for line in categories if not line.category), Decimal(0)
        ),
        reconciled_ratio=(coverage.reconciled / coverage.total) if coverage.total else None,
    )


def _categories(
    user_id: str,
    session: Session,
    *,
    start: datetime,
    end: datetime,
    last_start: datetime,
) -> list[CategoryLine]:
    last_by_category = {
        row.category: Decimal(row.total)
        for row in session.execute(
            _BY_CATEGORY, {"user_id": user_id, "start": last_start, "end": start}
        ).all()
    }
    return [
        CategoryLine(
            category=row.category,
            total=Decimal(row.total),
            count=row.count,
            last_total=last_by_category.get(row.category),
        )
        for row in session.execute(
            _BY_CATEGORY, {"user_id": user_id, "start": start, "end": end}
        ).all()
    ]


def _a_day_in_previous_month(start: datetime) -> datetime:
    """上个月的某一天。

    `start` 已经是当月一号零点,所以往回退一天就落在上个月的最后一天。
    **不减 30 天**:三月一日减 30 天会退到一月,于是"上个月"整个错位,
    而环比那一栏会静静地拿一月的数去比。
    """
    return start - timedelta(days=1)
