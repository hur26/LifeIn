"""账本的只读工具(L1)。**问答 agent 靠它回答"这个月花了多少"。**

[03 的 P2](../../docs/03-roadmap.md#p2--记账与账单自动化) 要求
"账单增删改与组合查询(按时间 / 类别 / 关键字),在企微里对话式完成"。
这个模块是**查询**那一半。

## 增删改那一半不在这里,这是刻意的

对话式的改账要先让模型理解"把昨天星巴克那笔改成餐饮"指的是哪一笔 ——
而**理解错了就是改错一笔账**,和 03 那条"误记率 = 0"直接冲突。
[铁律 7](../../AGENTS.md#1-铁律) 说拿不准时默认不动作,所以这条路是:

    你说"把昨天星巴克那笔改成餐饮"
      → agent 提一条待确认(它指出是哪一笔、改成什么)
      → 你在 App 或卡片上点一下
      → 那时才真的写

App 里点一下直接改是另一条路,不经过任何理解 —— **两条路各自都说得通,
而对话那条必须多一次点头**,因为它多了一次可能理解错的机会。

## 为什么是 L1

它只读。查账本不改任何东西,错了顶多答得不对 ——
而"答得不对"和"改错一笔账"是完全不同量级的两件事。

**但它读的是最私密的一张表。** 所以它和记忆那三个工具一样,
在 agent 白名单里单独列出来:问答能查账本,不代表别的 agent 也能
(网关那道"双重门")。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from pydantic import BaseModel, Field, model_validator

from lifein.governance.registry import ToolContext, ToolLevel, tool
from lifein.repos import budgets, reports, transactions
from lifein.repos.transactions import CATEGORIES

log = logging.getLogger(__name__)

MAX_ROWS = 50
"""一次最多回多少笔。**问答的答案要能读完** —— 五十笔已经超过了
一条消息该有的长度,更多只会让模型去总结,而总结意味着它要算钱。"""

DEFAULT_DAYS = 30


class LedgerQueryArgs(BaseModel):
    """组合查询:按时间、类别、关键字。三个都可以不给。"""

    since: datetime | None = None
    until: datetime | None = None
    category: str | None = None
    keyword: str | None = None
    """**只搜商户名。** 搜金额的话,问"38 块那笔"会连 3800 的房租一起回来。"""

    limit: int = Field(default=20, ge=1, le=MAX_ROWS)

    @model_validator(mode="after")
    def _check(self) -> LedgerQueryArgs:
        for name in ("since", "until"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} 必须带时区")
        if self.category is not None and self.category not in CATEGORIES:
            # 枚举外的类目查出来永远是空,而空结果会被模型解释成"你没花过这类钱"
            raise ValueError(f"分类不在枚举内:{self.category}")
        return self


class SpendingArgs(BaseModel):
    """某一段时间花了多少。**这个工具算,模型不算**(铁律 9)。"""

    period: str | None = None
    """`2026-08`。不给就是当月。"""


@tool(
    name="ledger.query",
    level=ToolLevel.L1,
    args=LedgerQueryArgs,
    summary="查账目:按时间、类别、商户关键字组合筛",
)
def query(args: LedgerQueryArgs, ctx: ToolContext) -> dict:
    """查账目。**金额原样返回字符串** —— 让模型去格式化数字,
    它会顺手把 38.50 写成 38.5 或者 39,而那两个都不是账本上的数。
    """
    _need_session(ctx)
    now = datetime.now(tz=(args.until or args.since or datetime.now().astimezone()).tzinfo)
    until = args.until or now
    since = args.since or (until - timedelta(days=DEFAULT_DAYS))

    items = transactions.search(
        ctx.user_id,
        ctx.session,
        start=since,
        end=until,
        category=args.category,
        keyword=args.keyword,
        limit=args.limit,
    )
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "count": len(items),
        "transactions": [
            {
                "id": item.id,
                "occurred_at": item.occurred_at.isoformat(),
                "amount": str(item.amount),
                "direction": item.direction.value,
                "kind": item.kind.value,
                "merchant": item.merchant_raw,
                "category": item.category,
            }
            for item in items
        ],
    }


@tool(
    name="ledger.spending",
    level=ToolLevel.L1,
    args=SpendingArgs,
    summary="某个月花了多少,按类目分",
)
def spending(args: SpendingArgs, ctx: ToolContext) -> dict:
    """一个月的合计与分类目明细。**数字在这里算好**,模型只负责念出来。

    分成两个工具而不是让模型拿 `ledger.query` 的结果自己求和:
    它求和会错,而**错了的那个数看起来和对的一模一样**。
    """
    _need_session(ctx)
    moment = _month_start(args.period)
    report = reports.monthly(ctx.user_id, ctx.session, now=moment)
    progress = budgets.progress(ctx.user_id, ctx.session, now=moment)

    return {
        "period": report.period,
        "total": str(report.total),
        "count": report.count,
        "last_total": str(report.last_total) if report.last_total is not None else None,
        "categories": [
            {"category": line.category, "total": str(line.total), "count": line.count}
            for line in report.categories
        ],
        "uncategorized": str(report.uncategorized),
        "budgets": [
            {
                "category": item.budget.category,
                "amount": str(item.budget.amount),
                "spent": str(item.spent),
                "over": item.over,
            }
            for item in progress
        ],
    }


def _month_start(period: str | None) -> datetime:
    if not period:
        return datetime.now().astimezone()
    try:
        year, month = (int(part) for part in period.split("-", 1))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"period 要写成 2026-08:{period}") from exc
    return datetime(year, month, 15, 12, 0).astimezone()


def _need_session(ctx: ToolContext) -> None:
    if ctx.session is None:
        raise RuntimeError("这个工具要碰库,调用方必须在 CallContext 里带上 session")
