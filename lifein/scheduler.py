"""触发层:进程内调度。

APScheduler,进程内,不引消息队列(ADR-016)。**不用它自带的持久化** ——
补偿靠 `job_runs` 里的窗口记录(ADR-016 那句"换调度器时不会丢补偿能力")。
所以这里配的是内存 jobstore,进程重启后调度状态归零,而该补的窗口一条不少。

**启动时先跑一次。** 不是为了立刻出摘要,是为了让补偿在重启后立即生效:
昨晚八点进程正好挂着,今早重启不该等到明晚八点才发现漏了一天。
`windows_to_run` 算不出窗口时它什么都不做,所以多跑几次没有副作用。

**一个用户失败不影响别人。** P0 只有一个用户,但这条现在写比 P4 再补便宜 ——
到那时"某个人的授权码过期导致所有人都收不到摘要"会是个很难解释的故障。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from lifein.bootstrap import Services, build_adapters, build_own_identifiers
from lifein.db import session_scope
from lifein.jobs.daily_digest import DigestDeps, run_once
from lifein.jobs.memory_extract import MemoryDeps
from lifein.jobs.memory_extract import run_once as run_memory_once
from lifein.jobs.plan_extract import PlanDeps
from lifein.jobs.plan_extract import run_once as run_plan_once
from lifein.repos import users

SessionFactory = Callable[[], AbstractContextManager[Session]]

log = logging.getLogger(__name__)

DIGEST_JOB_ID = "daily_digest"
MEMORY_JOB_ID = "memory_extract"
PLAN_JOB_ID = "plan_extract"

PLAN_DELAY_MINUTES = 45
"""日程提取排在摘要之后多久。

在记忆抽取(+30)之后再隔一刻钟,理由和记忆那条一样:它们都只读 raw_events,
而当天的事件是摘要那一步采进来的。彼此之间没有依赖 —— 错开只是为了
不在同一分钟里同时打三次外部模型接口。
"""

MEMORY_DELAY_MINUTES = 30
"""记忆抽取排在摘要之后多久。

**必须在后面。** 当天的事件是摘要那一步采进来的,记忆抽取只读 `raw_events`;
排在前面的话它永远看的是昨天,记忆会稳定地慢一天。

半小时是给采集留的余量:邮箱慢一点、日历接口抖一下都还在这个窗口内。
两个 job 各自认领自己的窗口,所以就算真的错开了,下一次也会补上。
"""


def run_digest_for_all_users(
    services: Services,
    *,
    now: datetime | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    """给每个未停用的用户跑一遍摘要。返回成功推送的人数。

    `session_factory` 可注入,默认用全局的 `session_scope`。留这个口子不是
    为了测试方便 —— 是因为"连哪个库"不该是一个藏在 import 里的全局决定,
    P4 做多租户或者临时跑一次别的库时都会用到。
    """
    open_session = session_factory or session_scope
    moment = now or datetime.now(UTC)
    pushed = 0

    with open_session() as session:
        user_ids = users.list_active_users(session)

    for user_id in user_ids:
        try:
            with open_session() as session:
                deps = DigestDeps(
                    adapters=build_adapters(user_id, session, services),
                    llm=services.llm,
                    channel=services.channel,
                    alerter=services.alerter,
                )
                results = run_once(user_id, session, deps=deps, now=moment)
            pushed += sum(1 for r in results if r.pushed)
        except Exception as exc:  # noqa: BLE001
            # 一个人失败不影响别人。每个用户各开一个事务,失败的那个整体回滚
            log.exception("用户 %s 的摘要任务失败", user_id)
            services.alerter.alert("摘要任务异常", f"user={user_id}: {type(exc).__name__}: {exc}")

    return pushed


def run_memory_extract_for_all_users(
    services: Services,
    *,
    now: datetime | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    """给每个未停用的用户抽一遍记忆。返回新写入的事实条数。

    和摘要那个函数是一样的形状,连"一个人失败不影响别人"都一样 ——
    区别只有一个:**抽取失败不告诉用户,只告警。** 记忆没更新当天没有任何
    外部表现,不该为它打扰用户;但运维要知道,不然它能悄悄停一个月。
    """
    open_session = session_factory or session_scope
    moment = now or datetime.now(UTC)
    written = 0

    with open_session() as session:
        user_ids = users.list_active_users(session)

    for user_id in user_ids:
        try:
            with open_session() as session:
                deps = MemoryDeps(
                    llm=services.llm,
                    alerter=services.alerter,
                    own_identifiers=build_own_identifiers(user_id, session, services),
                )
                results = run_memory_once(user_id, session, deps=deps, now=moment)
            written += sum(r.facts_created for r in results)
        except Exception as exc:  # noqa: BLE001
            log.exception("用户 %s 的记忆抽取失败", user_id)
            services.alerter.alert(
                "记忆抽取异常", f"user={user_id}: {type(exc).__name__}: {exc}"
            )

    return written


def run_plan_extract_for_all_users(
    services: Services,
    *,
    now: datetime | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    """给每个未停用的用户提一遍日程与待办。返回直接建出来的条数。

    **返回的不是"提取到多少"** —— 大部分会进待确认队列,那些不算已经发生的事。
    """
    open_session = session_factory or session_scope
    moment = now or datetime.now(UTC)
    created = 0

    with open_session() as session:
        user_ids = users.list_active_users(session)

    for user_id in user_ids:
        try:
            with open_session() as session:
                deps = PlanDeps(llm=services.llm, alerter=services.alerter)
                results = run_plan_once(user_id, session, deps=deps, now=moment)
            created += sum(r.created for r in results)
        except Exception as exc:  # noqa: BLE001
            log.exception("用户 %s 的日程提取失败", user_id)
            services.alerter.alert("日程提取异常", f"user={user_id}: {type(exc).__name__}: {exc}")

    return created


def build_scheduler(
    services: Services,
    *,
    runner: Callable[[Services], int] | None = None,
    memory_runner: Callable[[Services], int] | None = None,
    plan_runner: Callable[[Services], int] | None = None,
) -> BackgroundScheduler:
    """按配置建调度器。**不 start** —— 由调用方决定什么时候起。"""
    run = runner or run_digest_for_all_users
    run_memory = memory_runner or run_memory_extract_for_all_users
    run_plan = plan_runner or run_plan_extract_for_all_users
    hour, minute = services.settings.digest_hour_minute
    memory_hour, memory_minute = _shift(hour, minute, MEMORY_DELAY_MINUTES)
    plan_hour, plan_minute = _shift(hour, minute, PLAN_DELAY_MINUTES)

    scheduler = BackgroundScheduler(timezone=services.settings.tzinfo)
    scheduler.add_job(
        lambda: run(services),
        trigger=CronTrigger(hour=hour, minute=minute, timezone=services.settings.tzinfo),
        id=DIGEST_JOB_ID,
        name="每日摘要",
        # 睡眠唤醒后攒了几次触发,只跑一次:真正的补偿靠 job_runs,
        # 让调度器也补一遍只会重复认领同一个窗口
        coalesce=True,
        max_instances=1,
        # 错过一小时以内的照跑。超过一小时说明机器停了,那属于补偿的事
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        lambda: run_memory(services),
        trigger=CronTrigger(
            hour=memory_hour, minute=memory_minute, timezone=services.settings.tzinfo
        ),
        id=MEMORY_JOB_ID,
        name="记忆抽取",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        lambda: run_plan(services),
        trigger=CronTrigger(hour=plan_hour, minute=plan_minute, timezone=services.settings.tzinfo),
        id=PLAN_JOB_ID,
        name="日程与待办提取",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    return scheduler


def _shift(hour: int, minute: int, minutes: int) -> tuple[int, int]:
    """把时刻往后挪几分钟,跨过午夜也对。"""
    total = (hour * 60 + minute + minutes) % (24 * 60)
    return divmod(total, 60)
