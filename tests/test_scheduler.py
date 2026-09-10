"""调度的集成测试。

需要真实 PostgreSQL(见 conftest.py),因为"给每个用户跑一遍"这件事的
关键就在于用户从哪来、失败时事务怎么隔。

调度器本身不启动 —— 测的是它按配置建成了什么样,以及那个被调用的函数
在多用户下的行为。让后台线程真的睡到八点,测不出任何东西。
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

from lifein.alerts import CollectingAlerter
from lifein.bootstrap import Services
from lifein.config import Settings
from lifein.repos import users
from lifein.scheduler import (
    APPROVAL_JOB_ID,
    BOOKKEEPING_JOB_ID,
    COVERAGE_JOB_ID,
    DIGEST_JOB_ID,
    MEMORY_JOB_ID,
    MONTHLY_JOB_ID,
    PLAN_JOB_ID,
    RECONCILE_JOB_ID,
    build_scheduler,
    run_digest_for_all_users,
)
from tests.test_config import BASE

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 7, 8, tzinfo=UTC)


def services(**overrides) -> Services:
    settings = Settings(_env_file=None, **{**BASE, **overrides})
    return Services(
        settings=settings,
        llm=None,
        channel=None,
        alerter=CollectingAlerter(),
    )


class TestSchedulerShape:
    def test_trigger_comes_from_config(self):
        scheduler = build_scheduler(services(daily_digest_at="07:30"), runner=lambda _s: 0)
        job = scheduler.get_job(DIGEST_JOB_ID)

        assert isinstance(job.trigger, CronTrigger)
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields["hour"] == "7"
        assert fields["minute"] == "30"

    def test_timezone_comes_from_config(self):
        scheduler = build_scheduler(services(tz="Asia/Shanghai"), runner=lambda _s: 0)
        assert "Shanghai" in str(scheduler.get_job(DIGEST_JOB_ID).trigger.timezone)

    def test_missed_runs_are_coalesced(self):
        # 睡眠唤醒后攒了几次触发只跑一次:真正的补偿靠 job_runs,
        # 让调度器也补一遍只会重复认领同一个窗口
        scheduler = build_scheduler(services(), runner=lambda _s: 0)
        job = scheduler.get_job(DIGEST_JOB_ID)
        assert job.coalesce is True
        assert job.max_instances == 1

    def test_memory_extract_runs_after_the_digest(self):
        """记忆抽取必须排在摘要后面。

        当天的事件是摘要那一步采进来的,记忆只读 raw_events ——
        排在前面它永远看的是昨天,记忆会稳定地慢一天,而且没有任何表现。
        """
        scheduler = build_scheduler(
            services(daily_digest_at="07:30"), runner=lambda _s: 0, memory_runner=lambda _s: 0
        )
        fields = {f.name: str(f) for f in scheduler.get_job(MEMORY_JOB_ID).trigger.fields}
        assert (fields["hour"], fields["minute"]) == ("8", "0")

    def test_memory_extract_wraps_past_midnight(self):
        # 摘要设在 23:45 时,记忆抽取是第二天 0:15,不是 23:75
        scheduler = build_scheduler(
            services(daily_digest_at="23:45"), runner=lambda _s: 0, memory_runner=lambda _s: 0
        )
        fields = {f.name: str(f) for f in scheduler.get_job(MEMORY_JOB_ID).trigger.fields}
        assert (fields["hour"], fields["minute"]) == ("0", "15")

    def test_the_model_jobs_are_staggered(self):
        """几个 job 错开:它们之间没有依赖,错开只是为了不在同一分钟里
        同时打几次外部模型接口。

        记账排在最后是另一个理由:一天里最后几笔消费常常发生在晚上,
        早跑一刻钟的代价是那几笔要等到明天才入账。
        """
        scheduler = build_scheduler(
            services(daily_digest_at="08:00"),
            runner=lambda _s: 0,
            memory_runner=lambda _s: 0,
            plan_runner=lambda _s: 0,
            bookkeeping_runner=lambda _s: 0,
            reconcile_runner=lambda _s: 0,
            monthly_runner=lambda _s: 0,
            coverage_runner=lambda _s: 0,
        )
        times = {}
        for job_id in (
            DIGEST_JOB_ID, MEMORY_JOB_ID, PLAN_JOB_ID,
            BOOKKEEPING_JOB_ID, RECONCILE_JOB_ID, MONTHLY_JOB_ID, COVERAGE_JOB_ID,
        ):
            fields = {f.name: str(f) for f in scheduler.get_job(job_id).trigger.fields}
            times[job_id] = (fields["hour"], fields["minute"])

        assert times[DIGEST_JOB_ID] == ("8", "0")
        assert times[MEMORY_JOB_ID] == ("8", "30")
        assert times[PLAN_JOB_ID] == ("8", "45")
        assert times[BOOKKEEPING_JOB_ID] == ("9", "0")
        # 对账排在记账之后:反过来的话昨天那几笔还没入账,对账单里对应的行
        # 会被判成"实时那路漏了"补一笔新的,然后记账再记一遍,同一笔就有两条
        assert times[RECONCILE_JOB_ID] == ("9", "15")
        # 月报每天都触发,但一个月只发一次 —— 挡住重复的是 job_runs 的窗口
        # 认领,不是 cron 的日期字段:写 day=1 的话一号那天进程没起来就整月漏发
        assert times[MONTHLY_JOB_ID] == ("9", "30")
        # 巡检排在最后:它读的是别的 job 刚写下的数,排在前面看的永远是昨天,
        # 而 R8 要的是早期信号,晚一天就少一天
        assert times[COVERAGE_JOB_ID] == ("9", "45")

    def test_scheduler_is_not_started_by_the_builder(self):
        # 由调用方决定什么时候起 —— 测试里不该有后台线程
        assert build_scheduler(services(), runner=lambda _s: 0).running is False

    def test_no_persistent_jobstore(self):
        """不用 APScheduler 自带的持久化(ADR-016)。

        补偿靠 job_runs,换调度器时不会丢这个能力。
        APScheduler 的 jobstore 要 start 之后才实例化,所以这里断言的是
        "没有配过任何 jobstore" —— 配了才会有别的东西。
        """
        scheduler = build_scheduler(services(), runner=lambda _s: 0)
        assert scheduler._jobstores == {}  # noqa: SLF001


class TestRunForAllUsers:
    """session 工厂注入进去,不碰全局配置。"""

    @staticmethod
    def factory(session):
        @contextmanager
        def _open():
            yield session

        return _open

    def test_no_users_is_not_an_error(self, pg_session):
        assert (
            run_digest_for_all_users(services(), now=NOW, session_factory=self.factory(pg_session))
            == 0
        )

    def test_disabled_users_are_skipped(self, pg_session):
        pg_session.execute(
            text(
                "INSERT INTO users (display_name, wecom_userid, disabled_at) "
                "VALUES ('停用的', 'gone', now())"
            )
        )
        assert users.list_active_users(pg_session) == []
        assert (
            run_digest_for_all_users(services(), now=NOW, session_factory=self.factory(pg_session))
            == 0
        )

    def test_one_user_failing_does_not_stop_the_others(self, pg_session):
        """P0 只有一个用户,但"某个人的授权码过期导致所有人收不到摘要"
        在 P4 会是个很难解释的故障。"""
        for i in range(3):
            users.create_user(pg_session, display_name=f"u{i}", wecom_userid=f"w{i}")

        svc = services()
        # 没有 IMAP 凭据 + 没有 calendar_id,适配器是空的 → 摘要必然失败,
        # 但三个用户都会被走一遍,而不是第一个就中断
        assert run_digest_for_all_users(svc, now=NOW, session_factory=self.factory(pg_session)) == 0
        assert len(users.list_active_users(pg_session)) == 3


def test_master_key_shape_is_what_the_fixture_assumes():
    # 上面那些 services() 依赖 BASE 里的主密钥是合法的,这里钉一下
    assert len(base64.b64decode(BASE["master_key"])) == 32


def test_approvals_run_far_more_often_than_the_daily_jobs():
    """**点完同意之后等一整天才发出去,那条消息多半已经没意义了。**

    而"点了没反应"会让人下次不敢再点 —— 而 P3 的目标正是"你敢让它代你发
    一条真实消息"。所以它是间隔触发,不是每天一次。
    """
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = build_scheduler(services(), runner=lambda _s: 0)
    job = scheduler.get_job(APPROVAL_JOB_ID)

    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval.total_seconds() <= 600


def test_a_missed_approval_run_is_not_made_up():
    """**审批不该被补跑。** 一条几小时前批准的代发消息,补跑发出去时内容
    可能已经不合时宜 —— 和"超过 24 小时自动过期"是同一个道理。"""
    scheduler = build_scheduler(services(), runner=lambda _s: 0)
    job = scheduler.get_job(APPROVAL_JOB_ID)

    assert job.misfire_grace_time is not None
    assert job.misfire_grace_time <= 300


class TestEveryModelSpenderIsCapped:
    """**判据是"会不会调模型",不是"是不是定时任务"。**

    上限最初只挡住了定时任务那一半,而花钱最快的那条路根本没被挡:
    问答一次两到三次模型调用,而用户想问几次就问几次 ——
    一个人聊一下午,定时任务那边省下的几毛钱一次就还回去了。

    这一组盯的是**那张豁免清单是不是还成立**:新加一个会调模型的入口而忘了
    挂上限,下面第一条会红。
    """

    NO_MODEL = {
        # 对账回填不调模型:归类只查 merchant_rules(那个模块开头写着理由)
        "reconcile",
        # 下面这些一次模型调用都没有
        "coverage_watch",
        "approvals",
        "reminders",
        "collector_watch",
        "notification_retention",
    }
    """**豁免的理由只有一个**:这个入口一次模型调用都不会发生。

    改这份清单之前先回答一个问题:那个 job 现在调模型了吗?
    调了就不是加进来,是给它挂上限。
    """

    def runners(self) -> dict[str, str]:
        """`scheduler.py` 里每个 `run_*_for_all_users` 的源码。"""
        import inspect

        from lifein import scheduler

        return {
            name.removeprefix("run_").removesuffix("_for_all_users"): inspect.getsource(fn)
            for name, fn in vars(scheduler).items()
            if name.startswith("run_") and name.endswith("_for_all_users")
        }

    def test_every_runner_either_checks_quota_or_is_on_the_list(self):
        for job, source in self.runners().items():
            if "within_quota" in source:
                continue
            assert job in self.NO_MODEL, (
                f"{job} 既不查额度,也不在「它不调模型」那份清单里。"
                "确实不调模型的话,把它加进 NO_MODEL 并在那里写清理由;"
                "调模型的话给它挂上 within_quota —— 这一条挡的正是「忘了挂」。"
            )

    def test_the_exempt_ones_really_do_not_call_the_model(self):
        """**"它不调模型"这句话必须是被验证过的,不是被相信的。**

        清单本身会过期:某天有人给提醒加一句"让模型润色一下",而这份清单
        不会自己更新。所以直接去那个 job 的源码里找模型的影子。
        """
        import importlib
        import inspect

        modules = {
            "reconcile": "lifein.jobs.reconcile",
            "coverage_watch": "lifein.jobs.coverage_watch",
            "approvals": "lifein.jobs.approval_execute",
            "reminders": "lifein.jobs.reminders",
            "collector_watch": "lifein.jobs.collector_watch",
            "notification_retention": "lifein.jobs.notification_retention",
        }
        for job, name in modules.items():
            source = inspect.getsource(importlib.import_module(name))
            assert "LLMClient" not in source and "llm.chat" not in source, (
                f"{job} 现在会调模型了,但它还在豁免清单里 —— 那等于它不受成本上限管"
            )

    def test_every_place_that_builds_qadeps_caps_it(self):
        """**问答不是定时任务,但它一样花钱,而且花得最快。**

        **不点名具体哪个模块,而是去找"谁装配了 QaDeps"** —— 入站通道会变
        (企微那一路 2026-09-10 整个退出了,ADR-026),而"装配 QaDeps 的地方
        都要挂额度"这条规则不会变。点名模块的话,那个模块消失时这一条会
        以一种指错方向的方式红:它会说"api.app 没挂额度",而真相是
        api.app 根本不再收消息了。

        漏一处的表现是"从那个通道问就不花钱" ——
        那种漏法在账单上看得见、在代码里看不见。
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "lifein"
        builders = []
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            if "QaDeps(" in source and "def " in source:
                builders.append((path.relative_to(root).as_posix(), source))

        assert builders, "一个装配 QaDeps 的地方都没有?那问答根本跑不起来"
        for name, source in builders:
            assert "within_quota=" in source, f"{name} 装配 QaDeps 时没挂额度检查"
