"""内建规则的清单。

P1 选的头两条代表两类不同的主动推送,而那一期要验证的正是
"主动说话时不招人烦":

    upcoming_schedule   **有时限的事** —— 错过就没了,该提前说
    pending_backlog     **攒着的事** —— 不提也不会消失,所以宁可少说

P2 加的第三条是第三类:

    budget_alert        **已经发生的事** —— 说了也改不回来,但你得知道
                        (03 要求超支**当天**发出,所以它必须是规则,
                         不能是月度任务)

两条都从 `shadow` 起步(没有 `rule_state` 记录就是 shadow),先跑一周看
误报率,再决定转不转 active。03 的验收标准写着"主动提醒误报率 < 20%,
先跑一周影子模式统计" —— 那句话说的就是这里。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import timedelta

from lifein.channels.base import Card, CardSection
from lifein.repos import pending, todos
from lifein.rules.base import Reminder, Rule, RuleContext
from lifein.rules.budget import BUDGET_ALERT

log = logging.getLogger(__name__)

REMIND_BEFORE = timedelta(minutes=30)
"""日程提前多久提醒。

半小时是"还来得及出门"和"还没到该被打扰的时候"之间的折中。做成常量而不是
配置项:它该由用了一周之后的感受来定,而不是在第一天让人填一个数字。
"""

BACKLOG_REMIND_AT = 5
"""待确认攒到多少条才提醒一次。

比 `plan_extract.BACKLOG_ALERT_AT`(20)低得多,因为那条是给运维的告警
("判据太保守了"),这条是给用户的提醒("有几件事等你点一下")。
两个数字服务于不同的人,不该合并。
"""


def _upcoming_schedule(ctx: RuleContext) -> Sequence[Reminder]:
    """快到点的日程,提前半小时说一声。

    只看已经同步进系统日历的?**不**。没同步的更该提醒 —— 那说明手机端
    还没把它写进日历,系统日历自己的提醒不会响,这时候这条推送是唯一的防线。
    """
    upcoming = [
        item
        for item in todos.list_open(
            ctx.user_id, ctx.session, until=ctx.now + REMIND_BEFORE, limit=20
        )
        if item.kind is todos.TodoKind.SCHEDULE
        and item.starts_at is not None
        and item.starts_at > ctx.now
    ]
    if not upcoming:
        return []

    return [
        Reminder(
            card=Card(
                title="快到点了",
                summary=f"{_hhmm(item)} {item.title}",
                footer="来自你确认过的日程",
            ),
            dedup_key=item.id,
            # 一条日程只提醒一次:提前半小时说过了,还没去就不是提醒的问题了
            cooldown=timedelta(days=1),
        )
        for item in upcoming
    ]


def _hhmm(item: todos.Todo) -> str:
    return item.starts_at.strftime("%H:%M") if item.starts_at else ""


def _pending_backlog(ctx: RuleContext) -> Sequence[Reminder]:
    """待确认攒够了就提一次。

    **一天最多一次**(cooldown 一天,dedup_key 固定)。这条规则天然会每次扫描
    都命中 —— 队列不会自己变短 —— 所以冷却期是它唯一的刹车。
    """
    items = pending.list_pending(ctx.user_id, ctx.session, now=ctx.now, limit=BACKLOG_REMIND_AT)
    if len(items) < BACKLOG_REMIND_AT:
        return []

    return [
        Reminder(
            card=Card(
                title="有几件事等你确认",
                summary=f"待确认里攒了至少 {len(items)} 条,要不要清一下?",
                sections=[
                    CardSection(
                        heading="最早的几条",
                        lines=[str(p.payload.get("title", "(没有标题)")) for p in items[:3]],
                    )
                ],
            ),
            dedup_key="pending_backlog",
            cooldown=timedelta(days=1),
        )
    ]


UPCOMING_SCHEDULE = Rule(
    rule_id="upcoming_schedule",
    description="日程开始前半小时提醒一次",
    evaluate=_upcoming_schedule,
)

PENDING_BACKLOG = Rule(
    rule_id="pending_backlog",
    description=f"待确认攒到 {BACKLOG_REMIND_AT} 条时提醒一次,每天最多一次",
    evaluate=_pending_backlog,
)

ALL_RULES: tuple[Rule, ...] = (UPCOMING_SCHEDULE, PENDING_BACKLOG, BUDGET_ALERT)
"""加一条新规则只改这一行和一个新模块。**默认 shadow,不需要在这里声明。**"""
