"""job_runs 与补偿窗口的集成测试。

需要真实 PostgreSQL(见 conftest.py)。

补偿是那种"平时看不见、真出事时决定体验"的机制:进程半夜被 kill,第二天
早上重启,该补一条还是六十条,差别就在这几个用例上。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from lifein.repos.job_runs import (
    MAX_CATCHUP_WINDOWS,
    claim_window,
    finish_window,
    last_successful_window_end,
    windows_to_run,
)

pytestmark = pytest.mark.integration

JOB = "daily_digest"
NOW = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def claim(session, user_id, start, end=None):
    return claim_window(
        user_id, session, job_name=JOB, window_start=start, window_end=end or start + DAY
    )


def test_claiming_the_same_window_twice_fails_the_second_time(pg_session, user_id):
    """窗口是幂等键。补几次都是同一个结果。"""
    assert claim(pg_session, user_id, NOW - DAY) is True
    assert claim(pg_session, user_id, NOW - DAY) is False


def test_finish_records_stats(pg_session, user_id):
    claim(pg_session, user_id, NOW - DAY)
    finish_window(
        user_id,
        pg_session,
        job_name=JOB,
        window_start=NOW - DAY,
        status="succeeded",
        stats={"inserted": 12, "pushed": True},
    )
    row = pg_session.execute(
        text("SELECT status, stats, finished_at FROM job_runs WHERE job_name = :j"),
        {"j": JOB},
    ).one()
    assert row.status == "succeeded"
    assert row.stats["inserted"] == 12
    assert row.finished_at is not None


def test_finish_rejects_a_bogus_status(pg_session, user_id):
    claim(pg_session, user_id, NOW - DAY)
    with pytest.raises(ValueError):
        finish_window(
            user_id, pg_session, job_name=JOB, window_start=NOW - DAY, status="差不多好了"
        )


def test_a_killed_run_stays_visible_as_running(pg_session, user_id):
    """ "跑了一半"和"从来没跑过"是两种状态,分不清就没法安全补偿。"""
    claim(pg_session, user_id, NOW - DAY)
    # 不调 finish,模拟进程被 kill
    assert last_successful_window_end(user_id, pg_session, job_name=JOB) is None
    status = pg_session.execute(text("SELECT status FROM job_runs")).scalar_one()
    assert status == "running"


class TestWindowsToRun:
    def test_first_run_does_not_backfill_history(self, pg_session, user_id):
        # 回溯历史既慢,又会把一堆旧邮件当成"今天的事"推给你
        windows = windows_to_run(user_id, pg_session, job_name=JOB, now=NOW)
        assert windows == [(NOW - DAY, NOW)]

    def test_normal_day_produces_one_window(self, pg_session, user_id):
        start = NOW - 2 * DAY
        claim(pg_session, user_id, start, NOW - DAY)
        finish_window(user_id, pg_session, job_name=JOB, window_start=start, status="succeeded")
        windows = windows_to_run(user_id, pg_session, job_name=JOB, now=NOW)
        assert windows == [(NOW - DAY, NOW)]

    def test_downtime_is_compensated(self, pg_session, user_id):
        start = NOW - 3 * DAY
        claim(pg_session, user_id, start, NOW - 2 * DAY)
        finish_window(user_id, pg_session, job_name=JOB, window_start=start, status="succeeded")
        windows = windows_to_run(user_id, pg_session, job_name=JOB, now=NOW)
        assert len(windows) == 2
        assert windows[0][0] == NOW - 2 * DAY
        assert windows[-1][1] == NOW

    def test_long_downtime_is_capped(self, pg_session, user_id):
        """停机两个月,重启时不该往外发六十条摘要 —— 那比不发更糟。"""
        start = NOW - 60 * DAY
        claim(pg_session, user_id, start, NOW - 59 * DAY)
        finish_window(user_id, pg_session, job_name=JOB, window_start=start, status="succeeded")
        windows = windows_to_run(user_id, pg_session, job_name=JOB, now=NOW)
        assert len(windows) == MAX_CATCHUP_WINDOWS

    def test_failed_run_is_retried_next_time(self, pg_session, user_id):
        # 失败的窗口不算成功,下次还会被算进来
        start = NOW - 2 * DAY
        claim(pg_session, user_id, start, NOW - DAY)
        finish_window(
            user_id,
            pg_session,
            job_name=JOB,
            window_start=start,
            status="failed",
            error="LLM 超时",
        )
        assert last_successful_window_end(user_id, pg_session, job_name=JOB) is None

    def test_other_users_windows_are_invisible(self, pg_session, user_id):
        claim(pg_session, user_id, NOW - DAY)
        finish_window(user_id, pg_session, job_name=JOB, window_start=NOW - DAY, status="succeeded")
        other = "99999999-9999-9999-9999-999999999999"
        assert last_successful_window_end(other, pg_session, job_name=JOB) is None
