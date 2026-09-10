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
from decimal import Decimal

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session

from lifein import schema_guard
from lifein.bootstrap import Services, build_adapters, build_own_identifiers
from lifein.db import get_engine, session_scope
from lifein.jobs import notification_retention
from lifein.jobs.approval_execute import ExecuteDeps
from lifein.jobs.approval_execute import run_once as run_approvals_once
from lifein.jobs.bookkeeping import BookkeepingDeps
from lifein.jobs.bookkeeping import run_once as run_bookkeeping_once
from lifein.jobs.collector_watch import WatchDeps
from lifein.jobs.collector_watch import run_once as run_watch_once
from lifein.jobs.coverage_watch import CoverageDeps
from lifein.jobs.coverage_watch import run_once as run_coverage_once
from lifein.jobs.daily_digest import DigestDeps, run_once
from lifein.jobs.memory_extract import MemoryDeps
from lifein.jobs.memory_extract import run_once as run_memory_once
from lifein.jobs.monthly_report import MonthlyDeps
from lifein.jobs.monthly_report import run_once as run_monthly_once
from lifein.jobs.plan_extract import PlanDeps
from lifein.jobs.plan_extract import run_once as run_plan_once
from lifein.jobs.reconcile import ReconcileDeps
from lifein.jobs.reconcile import run_once as run_reconcile_once
from lifein.jobs.reminders import ReminderDeps
from lifein.jobs.reminders import run_once as run_reminders_once
from lifein.repos import quota, users

SessionFactory = Callable[[], AbstractContextManager[Session]]

log = logging.getLogger(__name__)

DIGEST_JOB_ID = "daily_digest"
MEMORY_JOB_ID = "memory_extract"
PLAN_JOB_ID = "plan_extract"
BOOKKEEPING_JOB_ID = "bookkeeping"
RECONCILE_JOB_ID = "reconcile"
MONTHLY_JOB_ID = "monthly_report"
COVERAGE_JOB_ID = "coverage_watch"
APPROVAL_JOB_ID = "approval_execute"
REMINDER_JOB_ID = "reminders"
COLLECTOR_JOB_ID = "collector_watch"
RETENTION_JOB_ID = "notification_retention"
SCHEMA_JOB_ID = "schema_check"

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

APPROVAL_INTERVAL_MINUTES = 3
"""审批执行多久跑一次。**比别的 job 勤得多。**

点完同意之后等一整天才发出去,那条消息多半已经没意义了;而"点了没反应"
会让人下次不敢再点 —— 而 P3 的目标正是"你敢让它代你发一条真实消息"。

三分钟是"感觉上立刻"和"不至于空转太多次"之间的折中。空跑一次的成本是
一次查库,而队列九成时间是空的。
"""

COVERAGE_DELAY_MINUTES = 105
"""覆盖率巡检排在最后。

它读的是别的 job 刚写下的数(记账的归类计数、对账的回填结果),所以排在
它们后面才看得到今天的情况 —— 排在前面的话,看的永远是昨天,
而 R8 要的是**早期**信号,晚一天就少一天。
"""

MONTHLY_DELAY_MINUTES = 90
"""月度报告排在摘要之后多久。**它每天都触发,但一个月只发一次** ——
挡住重复的是 `job_runs` 的窗口认领,不是 cron 的日期字段。

不写 `day=1` 是刻意的:那样一号那天进程没起来就整个月都补不回来了,
而月报是这个系统里最不该漏发的东西之一 —— 漏了不会有任何迹象。
每天来敲一次门,认领得到就发,认领不到就走人。
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



def within_quota(
    services: Services, session, user_id: str, *, now: datetime, job: str
) -> bool:
    """这个用户这个月还有额度吗。**没有就跳过这一轮,不算失败。**

    停的是模型调用不是采集(P4 第 5 片):采集几乎不花钱,而停掉它丢的数据
    补不回来 —— 手机上那条通知早被划掉了。所以超上限那天的表现是
    "没有摘要、没有新记忆",而**原文都还在,加回额度后能重跑**。

    **不告警给用户。** 他对"你这个月的模型账单到上限了"做不了任何事;
    要处理的是你,而你看的是告警通道。

    **问答那一条是例外**,它会回一句话 —— 因为他刚问了一句话,
    而对一个直接的提问保持沉默看起来像系统坏了(见 `repos/quota` 模块开头)。
    """
    cap = services.settings.monthly_cost_cap_cny if services.settings else 0.0
    if not cap:
        return True

    try:
        quota.guard(user_id, session, now=now, cap=Decimal(str(cap)))
    except quota.QuotaExceeded as exc:
        log.warning("跳过 %s:%s", job, exc)
        services.alerter.alert(f"{job} 因额度上限跳过", f"user={user_id}: {exc}")
        return False
    return True


def quota_checker(services: Services, *, job: str) -> Callable[[Session, str], bool]:
    """把 `within_quota` 包成问答那边要的形状:`(session, user_id) -> bool`。

    **问答不是定时任务,但它一样花钱** —— 而且花得最快:一次问答两到三次
    模型调用,用户想问几次就问几次。判据从来不是"是不是定时任务",
    是"会不会调模型"(`repos/quota` 里那张表)。

    `now` 在这里现取:问答是随时来的,没有一个"这一轮"的时刻可言。
    """

    def check(session: Session, user_id: str) -> bool:
        return within_quota(
            services, session, user_id, now=datetime.now(UTC), job=job
        )

    return check


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
                if not within_quota(
                    services, session, user_id, now=moment, job="daily_digest"
                ):
                    continue
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
                if not within_quota(
                    services, session, user_id, now=moment, job="memory_extract"
                ):
                    continue
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
                if not within_quota(
                    services, session, user_id, now=moment, job="plan_extract"
                ):
                    continue
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
                if not within_quota(
                    services, session, user_id, now=moment, job="bookkeeping"
                ):
                    continue
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



def run_monthly_report_for_all_users(
    services: Services,
    *,
    now: datetime | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    """给每个未停用的用户发上个月的报告。返回真的发出去了几份。

    **每天跑,一个月只发一次。** 挡住重复的是 `job_runs`,不是日期判断 ——
    判断"今天是不是一号"会在进程当天没起来时整月漏发,而月报漏了不会有
    任何迹象。
    """
    open_session = session_factory or session_scope
    moment = now or datetime.now(UTC)
    sent = 0

    with open_session() as session:
        user_ids = users.list_active_users(session)

    for user_id in user_ids:
        try:
            with open_session() as session:
                if not within_quota(
                    services, session, user_id, now=moment, job="monthly_report"
                ):
                    continue
                deps = MonthlyDeps(
                    llm=services.llm,
                    channel=services.channel,
                    alerter=services.alerter,
                    # 长版走邮件:推送通道有长度上限,而一份完整的月报一定会被截断
                    email=services.email_channel,
                )
                result = run_monthly_once(user_id, session, deps=deps, now=moment)
            sent += 1 if result.delivered else 0
        except Exception as exc:  # noqa: BLE001
            log.exception("用户 %s 的月度报告失败", user_id)
            services.alerter.alert("月度报告异常", f"user={user_id}: {type(exc).__name__}: {exc}")

    return sent



def run_coverage_watch_for_all_users(
    services: Services,
    *,
    now: datetime | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    """给每个未停用的用户看一遍那几个数。返回**有几个人的数字不对劲**。

    返回的不是"看了几个人":这个 job 每天都会把所有人看一遍,那个数字没有信息量。
    """
    open_session = session_factory or session_scope
    moment = now or datetime.now(UTC)
    unhealthy = 0

    with open_session() as session:
        user_ids = users.list_active_users(session)

    for user_id in user_ids:
        try:
            with open_session() as session:
                report = run_coverage_once(
                    user_id, session, deps=CoverageDeps(alerter=services.alerter), now=moment
                )
            unhealthy += 0 if report.healthy else 1
        except Exception as exc:  # noqa: BLE001
            log.exception("用户 %s 的覆盖率巡检失败", user_id)
            services.alerter.alert(
                "覆盖率巡检异常", f"user={user_id}: {type(exc).__name__}: {exc}"
            )

    return unhealthy



def run_approvals_for_all_users(
    services: Services,
    *,
    now: Callable[[], datetime] | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    """把点过同意的 L3 调用做掉。返回真的执行了几条。

    **它跑得比别的 job 勤。** 别的都是一天一次,这个几分钟一次 ——
    因为点完同意之后等一整天才发出去,那条消息多半已经没意义了,
    而"点了没反应"会让人下次不敢再点(而 P3 的目标正是"你敢点")。
    """
    open_session = session_factory or session_scope
    clock = now or (lambda: datetime.now(UTC))
    executed = 0

    with open_session() as session:
        user_ids = users.list_active_users(session)

    for user_id in user_ids:
        try:
            with open_session() as session:
                result = run_approvals_once(
                    user_id,
                    session,
                    deps=ExecuteDeps(
                        channel=services.channel, alerter=services.alerter, now=clock
                    ),
                )
            executed += result.executed
        except Exception as exc:  # noqa: BLE001
            log.exception("用户 %s 的审批执行失败", user_id)
            services.alerter.alert(
                "审批执行异常", f"user={user_id}: {type(exc).__name__}: {exc}"
            )

    return executed


def run_schema_check(services: Services) -> int:
    """核对库版本,落后就升到 head。返回这次真的应用了几个版本(ADR-030)。

    **它是这里唯一不按用户分的 job。** 别的都是"给每个用户跑一遍",而库结构
    是整个进程共用的一件事 —— 所以这里没有 `user_id`,也不认领 `job_runs`
    的窗口:补跑一次昨天的版本校验没有意义,今天这次看到的就是现在的状态。

    **排在空闲时段**(`SCHEMA_CHECK_AT`,默认 04:00)。它可能真的去改表结构,
    而那件事不该和摘要、记账那几个撞在一起。

    需要这一次的理由是 `migrations/versions/*.py` **是 alembic 运行时从磁盘
    读的**:一次 `git pull` 不重启进程,磁盘上的 head 就已经前进了,
    而那种漂移只有重启才会暴露 —— 这个进程可能几周不重启。
    """
    engine = get_engine(services.settings)
    try:
        state = schema_guard.inspect_schema(engine)
    except Exception as exc:  # noqa: BLE001 —— 连不上库、找不到 alembic.ini,都只是"这次没查成"
        log.exception("库版本自检失败")
        services.alerter.alert("库版本自检异常", f"{type(exc).__name__}: {exc}")
        return 0

    if state.unknown:
        # **开着自动升级也不升。** 自动升级的前提是"这些迁移本来就是这次部署
        # 带来的",而认不出来的版本不满足那个前提(ADR-030)
        services.alerter.alert(
            "库版本对不上", state.describe() + " —— 这一种不会自动处理,要人来看"
        )
        return 0

    if state.is_current:
        log.info("库版本自检:%s", state.describe())
        return 0

    if not services.settings.schema_auto_upgrade:
        services.alerter.alert(
            "库版本落后",
            state.describe() + " —— SCHEMA_AUTO_UPGRADE 是关的,要人工跑 alembic upgrade head",
        )
        return 0

    log.warning("库版本落后,开始自动升级:%s", state.describe())
    try:
        upgraded = schema_guard.upgrade_to_head(engine)
    except Exception as exc:  # noqa: BLE001 —— 迁移失败整条回滚,库还停在原来那个版本
        log.exception("库版本自动升级失败")
        services.alerter.alert(
            "库版本自动升级失败",
            state.describe() + "\n" + f"{type(exc).__name__}: {exc}",
        )
        return 0

    if not upgraded.is_current:
        # 没抢到锁(别的进程正在升)也会走到这里 —— 那不是失败,下一次会看到最新
        services.alerter.alert("库版本自动升级没做完", upgraded.describe())
        return 0

    detail = f"{state.current or '空库'} → {upgraded.head},应用了 {len(state.pending)} 个版本:" + (
        "、".join(reversed(state.pending))
    )
    started = schema_guard.startup_head()
    if started is not None and started != upgraded.head:
        # 库跟上了磁盘上的代码,而这个进程还跑着启动时那一份 —— 那是另一头的漂移
        detail += "\n\n" + f"磁盘上的代码比这个进程新(启动时 head 是 {started}),重启它。"
    # **成功也告警。** 一次无人值守的结构变更不该只留在日志里
    services.alerter.alert("库版本已自动升级", detail)
    return len(state.pending)


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
    monthly_runner: Callable[[Services], int] | None = None,
    coverage_runner: Callable[[Services], int] | None = None,
    approval_runner: Callable[[Services], int] | None = None,
    reminder_runner: Callable[[Services], int] | None = None,
    collector_runner: Callable[[Services], int] | None = None,
    retention_runner: Callable[[Services], int] | None = None,
    schema_runner: Callable[[Services], int] | None = None,
) -> BackgroundScheduler:
    """按配置建调度器。**不 start** —— 由调用方决定什么时候起。"""
    run = runner or run_digest_for_all_users
    run_memory = memory_runner or run_memory_extract_for_all_users
    run_plan = plan_runner or run_plan_extract_for_all_users
    run_bookkeeping = bookkeeping_runner or run_bookkeeping_for_all_users
    run_reconcile = reconcile_runner or run_reconcile_for_all_users
    run_monthly = monthly_runner or run_monthly_report_for_all_users
    run_coverage = coverage_runner or run_coverage_watch_for_all_users
    run_approvals = approval_runner or run_approvals_for_all_users
    run_reminders = reminder_runner or run_reminders_for_all_users
    run_collector_watch = collector_runner or run_collector_watch_for_all_users
    run_retention = retention_runner or run_notification_retention_for_all_users
    run_schema = schema_runner or run_schema_check
    hour, minute = services.settings.digest_hour_minute
    memory_hour, memory_minute = _shift(hour, minute, MEMORY_DELAY_MINUTES)
    plan_hour, plan_minute = _shift(hour, minute, PLAN_DELAY_MINUTES)
    book_hour, book_minute = _shift(hour, minute, BOOKKEEPING_DELAY_MINUTES)
    recon_hour, recon_minute = _shift(hour, minute, RECONCILE_DELAY_MINUTES)
    monthly_hour, monthly_minute = _shift(hour, minute, MONTHLY_DELAY_MINUTES)
    cov_hour, cov_minute = _shift(hour, minute, COVERAGE_DELAY_MINUTES)
    retention_hour, retention_minute = _shift(hour, minute, RETENTION_DELAY_MINUTES)
    # **不跟着摘要走。** 别的 job 排的都是"摘要之后第几分钟",而这一个要的是
    # 空闲,和摘要几点跑没有关系 —— 它自己配一个时刻(ADR-030)
    schema_hour, schema_minute = services.settings.schema_check_hour_minute

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
        lambda: run_monthly(services),
        trigger=CronTrigger(
            hour=monthly_hour, minute=monthly_minute, timezone=services.settings.tzinfo
        ),
        id=MONTHLY_JOB_ID,
        name="月度报告",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        lambda: run_coverage(services),
        trigger=CronTrigger(hour=cov_hour, minute=cov_minute, timezone=services.settings.tzinfo),
        id=COVERAGE_JOB_ID,
        name="覆盖率巡检",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        lambda: run_approvals(services),
        trigger=IntervalTrigger(minutes=APPROVAL_INTERVAL_MINUTES),
        id=APPROVAL_JOB_ID,
        name="审批执行",
        coalesce=True,
        max_instances=1,
        # 错过就等下一轮,不补。**审批不该被补跑** —— 一条几小时前批准的
        # 代发消息,补跑发出去时内容可能已经不合时宜(和过期那条同一个道理)
        misfire_grace_time=120,
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
    scheduler.add_job(
        lambda: run_schema(services),
        trigger=CronTrigger(
            hour=schema_hour, minute=schema_minute, timezone=services.settings.tzinfo
        ),
        id=SCHEMA_JOB_ID,
        name="库版本自检",
        coalesce=True,
        max_instances=1,
        # 错过一小时以内的照跑。这件事晚一点做没关系,不做才有关系
        misfire_grace_time=3600,
    )
    return scheduler


def _shift(hour: int, minute: int, minutes: int) -> tuple[int, int]:
    """把时刻往后挪几分钟,跨过午夜也对。"""
    total = (hour * 60 + minute + minutes) % (24 * 60)
    return divmod(total, 60)
