"""每用户的成本上限(P4 第 5 片)。

[03 的 P4 范围](../../docs/03-roadmap.md#p4--多用户托管):

> 每用户配额与**成本上限**(主动扫描的开销随用户数线性增长)

那句括号是要害:摘要、记忆抽取、日程提取、记账判定 —— **每个用户每天都要跑一遍**,
而它们全都调模型。一个人的时候一天几毛钱;十个人就是十份,
而**其中九份的账单是你付**。

## 停的是模型调用,不是采集

这个选择决定了超上限那天朋友看到什么:

| 停哪个 | 那天的表现 | 数据 |
| --- | --- | --- |
| **模型调用**(选它) | 没有摘要、没有新记忆、账目进待确认 | **原文还在**,加回额度后能重跑 |
| 采集 | App 里什么都没有 | **通知从手机上过去了就没了** |

采集是一次数据库插入,几乎不花钱;而停掉它丢的数据**补不回来** ——
手机上那条通知早被划掉了。所以上限卡在花钱的那一头。

## 花了多少不用另外记

`tool_calls` 每一条都带 `cost_cny`(`llm/client.py` 按配置的单价算好的),
所以"这个月花了多少"是一条 `sum` —— **不需要第二张计数表**。

多一张表意味着多一处可能和事实对不上的地方,而**对不上的那次一定是
"记的比实际少"**:漏记一次的代价是超了不知道。

## 上限是软的,不是硬的

超了之后**已经在跑的那一轮跑完**,下一次调用才被拦。做成硬的要在
每次调用前后各查一次库,而那两次查询本身比它省下的那半次调用还贵。

## 谁受它管:所有会调模型的入口,一个不漏

上限最初只挡住了定时任务那一半,而**花钱最快的那条路根本没被挡**:
企微里问一句话会走两到三次模型调用,而问答是**用户想问几次就问几次**的。
一个人聊一下午,定时任务那边省下的几毛钱一次就还回去了。

所以判据不是"它是不是定时任务",是**它会不会调模型**:

| 入口 | 调模型吗 | 受不受上限管 |
| --- | --- | --- |
| 摘要、记忆抽取、日程提取、记账判定、月报 | 是 | **是** |
| **问答(企微/微信收到消息)** | 是 | **是**(补上的那一条) |
| 对账回填 | 否(归类只查 `merchant_rules`) | 否 |
| 提醒、审批执行、审批回复、采集看门狗、正文保留期 | 否 | 否 |

**"不调模型所以不管"这句话必须是被验证过的**,不是被相信的 ——
`tests/test_scheduler.py` 里有一条盯着它:新加一个会调模型的入口而忘了
挂上限,那条会红。

## 问答超上限时会回一句话,别的入口不会

别处的规矩是"不告警给用户,他做不了任何事"。**问答这一条是例外,
因为他刚问了一句话** —— 而对一个直接的提问保持沉默,比说一句
"这个月的额度用完了"糟得多:前者看起来是系统坏了。

**审批指令("同意 12")不受上限管。** 它一次模型调用都不需要,而挡住它
意味着一条已经排队等着发的消息**永远发不出去** —— 那是把一个成本问题
变成了一个功能故障。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifein.repos.budgets import period_bounds

log = logging.getLogger(__name__)

WARN_AT = Decimal("0.8")
"""花到几成时提醒。**这一条走告警,不走推送** ——
用户对"你这个月的模型账单快到上限了"做不了任何事,那是运维信息。"""


@dataclass(frozen=True)
class Usage:
    spent: Decimal
    cap: Decimal | None
    calls: int
    period: str

    @property
    def over(self) -> bool:
        """**没设上限就永远不算超。** 默认无限,而不是默认一个猜出来的数字 ——
        猜出来的上限会在某个月安静地停掉一个人的全部功能。"""
        return self.cap is not None and self.spent >= self.cap

    @property
    def near(self) -> bool:
        return (
            self.cap is not None
            and not self.over
            and self.cap > 0
            and self.spent / self.cap >= WARN_AT
        )

    @property
    def remaining(self) -> Decimal | None:
        return None if self.cap is None else self.cap - self.spent


_SPENT = text("""
    SELECT
        COALESCE(sum(cost_cny), 0) AS spent,
        count(*) FILTER (WHERE cost_cny IS NOT NULL) AS calls
      FROM tool_calls
     WHERE user_id = :user_id
       AND created_at >= :start AND created_at < :end
""")


def usage(
    user_id: str, session: Session, *, now: datetime, cap: Decimal | None = None
) -> Usage:
    """这个月花了多少。**按自然月切**,和预算那边同一个口径。

    口径不一样的话,"这个月"在两个地方指不同的时间段,而那时没有人能
    对上账 —— 和 `repos/reports.py` 开头那条同一个理由。
    """
    start, end = period_bounds(now)
    row = session.execute(
        _SPENT, {"user_id": user_id, "start": start, "end": end}
    ).one()
    return Usage(
        spent=Decimal(row.spent),
        cap=cap,
        calls=int(row.calls),
        period=start.strftime("%Y-%m"),
    )


class QuotaExceeded(RuntimeError):
    """这个月的模型额度用完了。

    **调用方要把它当成"这一轮跳过",不是"系统坏了"** ——
    原文都还在,加回额度之后重跑一遍就补上了。
    """


def guard(user_id: str, session: Session, *, now: datetime, cap: Decimal | None) -> Usage:
    """调模型之前问一句。超了就抛。

    **上限是软的**(见模块开头):这里只在一轮的开头查一次,
    所以那一轮里的几次调用都会跑完。做成硬的要在每次调用前后各查一次库,
    而那两次查询本身比它省下的那半次调用还贵。
    """
    current = usage(user_id, session, now=now, cap=cap)
    if current.over:
        raise QuotaExceeded(
            f"{current.period} 的模型额度用完了(花了 {current.spent},上限 {cap})。"
            "采集没停,原文都在 —— 加回额度之后重跑一遍就补上了"
        )
    if current.near:
        log.warning(
            "用户 %s 的模型花费到了 %s(上限 %s)", user_id, current.spent, cap
        )
    return current
