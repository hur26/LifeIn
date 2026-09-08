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
    DIGEST_JOB_ID,
    MEMORY_JOB_ID,
    PLAN_JOB_ID,
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

    def test_three_jobs_are_staggered(self):
        """三个 job 错开:它们之间没有依赖,错开只是为了不在同一分钟里
        同时打三次外部模型接口。"""
        scheduler = build_scheduler(
            services(daily_digest_at="08:00"),
            runner=lambda _s: 0,
            memory_runner=lambda _s: 0,
            plan_runner=lambda _s: 0,
        )
        times = {}
        for job_id in (DIGEST_JOB_ID, MEMORY_JOB_ID, PLAN_JOB_ID):
            fields = {f.name: str(f) for f in scheduler.get_job(job_id).trigger.fields}
            times[job_id] = (fields["hour"], fields["minute"])

        assert times[DIGEST_JOB_ID] == ("8", "0")
        assert times[MEMORY_JOB_ID] == ("8", "30")
        assert times[PLAN_JOB_ID] == ("8", "45")

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
