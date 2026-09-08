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
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session

from lifein.bootstrap import Services, build_adapters, build_own_identifiers
from lifein.db import session_scope
from lifein.jobs import notification_retention
from lifein.jobs.bookkeeping import BookkeepingDeps
from lifein.jobs.bookkeeping import run_once as run_bookkeeping_once
from lifein.jobs.collector_watch import WatchDeps
from lifein.jobs.collector_watch import run_once as run_watch_once
from lifein.jobs.daily_digest import DigestDeps, run_once
from lifein.jobs.memory_extract import MemoryDeps
from lifein.jobs.memory_extract import run_once as run_memory_once
from lifein.jobs.plan_extract import PlanDeps
from lifein.jobs.plan_extract import run_once as run_plan_once
from lifein.jobs.reconcile import ReconcileDeps
from lifein.jobs.reconcile import run_once as run_reconcile_once
from lifein.jobs.reminders import ReminderDeps
from lifein.jobs.reminders import run_once as run_reminders_once
from lifein.repos import users

SessionFactory = Callable[[], AbstractContextManager[Session]]

log = logging.getLogger(__name__)

DIGEST_JOB_ID = "daily_digest"
MEMORY_JOB_ID = "memory_extract"
PLAN_JOB_ID = "plan_extract"
BOOKKEEPING_JOB_ID = "bookkeeping"
RECONCILE_JOB_ID = "reconcile"
REMINDER_JOB_ID = "reminders"
COLLECTOR_JOB_ID = "collector_watch"
RETENTION_JOB_ID = "notification_retention"

RETENTION_DELAY_MINUTES = 60
"""通知保留期清理排在摘要之后一小时。

**必须排在提取之后**(+45):清掉的是提取要读的那份正文。同一天里
先清后提,提取看到的就是空的 —— 而它不会报错,只会少提几条。"""

COLLECTOR_WATCH_INTERVAL_MINUTES = 5
"""采集器掉线扫描的间隔。

比提醒那个还密,因为它服务的是一条**验收标准**:掉线要在 1 小时内告警。
超时 60 分钟 + 这里 5 分钟 = 最晚 65 分钟发出;要严格卡进一小时,
把 COLLECTOR_HEARTBEAT_TIMEOUT_M 调到 55。

它一次只读两张小表、不调模型,密一点没有成本。
"""

REMINDER_INTERVAL_MINUTES = 15
"""提醒扫描的间隔。

**这个 job 和别的三个不一样:它按间隔跑,不按每天一次。** 提醒是有时效的,
"日程前半小时"这件事一天扫一次根本赶不上。

十五分钟是精度和成本的折中:提前半小时的提醒最多会晚十五分钟发出去,
而它一天只查两次库、不调模型,几乎没有成本。它也**不认领窗口** ——
补跑一个两小时前的提醒没有意义,那正是 job_runs 那套补偿不适用的场景。
"""

RECONCILE_DELAY_MINUTES = 75
"""对账排在记账之后多久。

**顺序有意义,不像另外几个。** 记账那一步可能刚把昨天的通知落成交易,
而对账要拿对账单去匹配已有的实时记录 —— 反过来的话,昨天那几笔还没入账,
对账单里对应的行会被判成"实时那路漏了",补成一笔新的,
然后记账那一步再把通知记一遍,同一笔就有了两条。

不靠这个顺序保证正确性(补录走 UNIQUE、回填走 matched_statement_event_id,
两道幂等都在),但靠它避免那种"每次都要靠幂等兜住"的日常。
"""

BOOKKEEPING_DELAY_MINUTES = 60
"""记账排在摘要之后多久。

排在最后面,和别的 job 一样只是为了不在同一分钟里打几次模型接口。
**但它比另外两个更该往后放**:一天里最后几笔消费常常发生在晚上,
而早跑一刻钟的代价是那几笔要等到明天才入账,月底那几天尤其明显。

它照样认领窗口,所以真错过了下一次会补上 —— 往后放不会漏,只会晚。
"""

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



def run_bookkeeping_for_all_users(
    services: Services,
    *,
    now: datetime | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    """给每个未停用的用户记一遍账。返回**真的新增了多少笔**。

    合并进已有那笔的不算,进待确认的也不算 —— 前者账本上没多出东西,
    后者还没发生。返回一个偏大的数会让"记账在干活"这件事看起来比实际好,
    而这个 job 恰恰是最不该被高估的那个(03 的验收标准是误记率 = 0)。
    """
    open_session = session_factory or session_scope
    moment = now or datetime.now(UTC)
    recorded = 0

    with open_session() as session:
        user_ids = users.list_active_users(session)

    for user_id in user_ids:
        try:
            with open_session() as session:
                deps = BookkeepingDeps(llm=services.llm, alerter=services.alerter)
                results = run_bookkeeping_once(user_id, session, deps=deps, now=moment)
            recorded += sum(r.recorded for r in results)
        except Exception as exc:  # noqa: BLE001
            log.exception("用户 %s 的记账失败", user_id)
            services.alerter.alert("记账异常", f"user={user_id}: {type(exc).__name__}: {exc}")

    return recorded



def run_reconcile_for_all_users(
    services: Services,
    *,
    session_factory: SessionFactory | None = None,
) -> int:
    """给每个未停用的用户对一遍账。返回回填了多少笔。

    **不收 `now`。** 它处理的是"还没处理过的对账单行",而那个集合和现在
    几点没有关系 —— 收一个用不上的时间会让人以为重跑的结果跟时间有关。
    """
    open_session = session_factory or session_scope
    backfilled = 0

    with open_session() as session:
        user_ids = users.list_active_users(session)

    for user_id in user_ids:
        try:
            with open_session() as session:
                deps = ReconcileDeps(alerter=services.alerter)
                result = run_reconcile_once(user_id, session, deps=deps)
            backfilled += result.backfilled
        except Exception as exc:  # noqa: BLE001
            log.exception("用户 %s 的对账失败", user_id)
            services.alerter.alert("对账异常", f"user={user_id}: {type(exc).__name__}: {exc}")

    return backfilled


def run_reminders_for_all_users(
    services: Services,
    *,
    now: datetime | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    """跑一遍主动提醒规则。返回真的推出去的条数。

    大部分时候返回 0 —— 规则默认在影子模式,而影子记录不算推送。
    """
    open_session = session_factory or session_scope
    moment = now or datetime.now(UTC)
    pushed = 0

    with open_session() as session:
        user_ids = users.list_active_users(session)

    for user_id in user_ids:
        try:
            with open_session() as session:
                deps = ReminderDeps(channel=services.channel, alerter=services.alerter)
                pushed += run_reminders_once(user_id, session, deps=deps, now=moment).pushed
        except Exception as exc:  # noqa: BLE001
            log.exception("用户 %s 的提醒任务失败", user_id)
            services.alerter.alert("提醒任务异常", f"user={user_id}: {type(exc).__name__}: {exc}")

    return pushed


def run_collector_watch_for_all_users(
    services: Services,
    *,
    now: datetime | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    """扫一遍所有用户的采集设备。返回这一轮新发出去的告警条数。

    **正常情况下永远返回 0。** 它不是"跑出东西才算有用"的那种任务 ——
    它存在的意义是掉线的那一天你能在一小时内知道。
    """
    open_session = session_factory or session_scope
    moment = now or datetime.now(UTC)
    alerted = 0

    with open_session() as session:
        user_ids = users.list_active_users(session)

    for user_id in user_ids:
        try:
            with open_session() as session:
                deps = WatchDeps(
                    alerter=services.alerter,
                    timeout_minutes=services.settings.collector_heartbeat_timeout_m,
                )
                result = run_watch_once(user_id, session, deps=deps, now=moment)
            alerted += len(result.alerted)
        except Exception as exc:  # noqa: BLE001
            log.exception("用户 %s 的采集器巡检失败", user_id)
            services.alerter.alert("采集器巡检异常", f"user={user_id}: {type(exc).__name__}: {exc}")

    return alerted


def run_notification_retention_for_all_users(
    services: Services,
    *,
    now: datetime | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    """给每个用户清一遍过期的通知正文。返回处理条数。

    失败只告警不打扰用户:没清成当天没有任何外部表现,但它是 R10 的措施,
    悄悄停一个月就等于那条措施不存在了。
    """
    open_session = session_factory or session_scope
    moment = now or datetime.now(UTC)
    cleaned = 0

    with open_session() as session:
        user_ids = users.list_active_users(session)

    for user_id in user_ids:
        try:
            with open_session() as session:
                cleaned += notification_retention.run_once(
                    user_id,
                    session,
                    now=moment,
                    retention_days=services.settings.notification_retention_days,
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("用户 %s 的通知清理失败", user_id)
            services.alerter.alert("通知保留期清理异常", f"user={user_id}: {exc}")

    return cleaned


def build_scheduler(
    services: Services,
    *,
    runner: Callable[[Services], int] | None = None,
    memory_runner: Callable[[Services], int] | None = None,
    plan_runner: Callable[[Services], int] | None = None,
    bookkeeping_runner: Callable[[Services], int] | None = None,
    reconcile_runner: Callable[[Services], int] | None = None,
    reminder_runner: Callable[[Services], int] | None = None,
    collector_runner: Callable[[Services], int] | None = None,
    retention_runner: Callable[[Services], int] | None = None,
) -> BackgroundScheduler:
    """按配置建调度器。**不 start** —— 由调用方决定什么时候起。"""
    run = runner or run_digest_for_all_users
    run_memory = memory_runner or run_memory_extract_for_all_users
    run_plan = plan_runner or run_plan_extract_for_all_users
    run_bookkeeping = bookkeeping_runner or run_bookkeeping_for_all_users
    run_reconcile = reconcile_runner or run_reconcile_for_all_users
    run_reminders = reminder_runner or run_reminders_for_all_users
    run_collector_watch = collector_runner or run_collector_watch_for_all_users
    run_retention = retention_runner or run_notification_retention_for_all_users
    hour, minute = services.settings.digest_hour_minute
    memory_hour, memory_minute = _shift(hour, minute, MEMORY_DELAY_MINUTES)
    plan_hour, plan_minute = _shift(hour, minute, PLAN_DELAY_MINUTES)
    book_hour, book_minute = _shift(hour, minute, BOOKKEEPING_DELAY_MINUTES)
    recon_hour, recon_minute = _shift(hour, minute, RECONCILE_DELAY_MINUTES)
    retention_hour, retention_minute = _shift(hour, minute, RETENTION_DELAY_MINUTES)

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
    scheduler.add_job(
        lambda: run_bookkeeping(services),
        trigger=CronTrigger(hour=book_hour, minute=book_minute, timezone=services.settings.tzinfo),
        id=BOOKKEEPING_JOB_ID,
        name="记账",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        lambda: run_reconcile(services),
        trigger=CronTrigger(
            hour=recon_hour, minute=recon_minute, timezone=services.settings.tzinfo
        ),
        id=RECONCILE_JOB_ID,
        name="对账回填",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        lambda: run_reminders(services),
        trigger=IntervalTrigger(minutes=REMINDER_INTERVAL_MINUTES),
        id=REMINDER_JOB_ID,
        name="主动提醒",
        coalesce=True,
        max_instances=1,
        # 错过五分钟以上就不补了:一条晚了半小时的"快到点了"只会让人困惑
        misfire_grace_time=300,
    )
    scheduler.add_job(
        lambda: run_retention(services),
        trigger=CronTrigger(
            hour=retention_hour, minute=retention_minute, timezone=services.settings.tzinfo
        ),
        id=RETENTION_JOB_ID,
        name="通知原文保留期清理",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        lambda: run_collector_watch(services),
        trigger=IntervalTrigger(minutes=COLLECTOR_WATCH_INTERVAL_MINUTES),
        id=COLLECTOR_JOB_ID,
        name="采集器掉线巡检",
        coalesce=True,
        max_instances=1,
        # 睡醒之后立刻补一次:机器停着的这段时间正是最可能掉线的时候
        misfire_grace_time=300,
    )
    return scheduler


def _shift(hour: int, minute: int, minutes: int) -> tuple[int, int]:
    """把时刻往后挪几分钟,跨过午夜也对。"""
    total = (hour * 60 + minute + minutes) % (24 * 60)
    return divmod(total, 60)
