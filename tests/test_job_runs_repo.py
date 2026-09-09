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
    MAX_ATTEMPTS,
    MAX_CATCHUP_WINDOWS,
    STALE_RUNNING_AFTER,
    claim_window,
    finish_window,
    last_successful_window_end,
    retryable_windows,
    windows_to_run,
)

pytestmark = pytest.mark.integration

JOB = "daily_digest"
NOW = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def claim(session, user_id, start, end=None, now=NOW):
    return claim_window(
        user_id,
        session,
        job_name=JOB,
        window_start=start,
        window_end=end or start + DAY,
        now=now,
    )


def fail(session, user_id, start, error="LLM 超时"):
    finish_window(
        user_id, session, job_name=JOB, window_start=start, status="failed", error=error
    )


def succeed(session, user_id, start):
    finish_window(user_id, session, job_name=JOB, window_start=start, status="succeeded")


def test_claiming_a_running_window_again_fails(pg_session, user_id):
    """还在跑的窗口拿不到第二次 —— 幂等键挡的是**并发**,不是重试。"""
    assert claim(pg_session, user_id, NOW - DAY) is True
    assert claim(pg_session, user_id, NOW - DAY) is False


def test_a_succeeded_window_is_never_claimed_again(pg_session, user_id):
    """干过的活不重复干。"""
    claim(pg_session, user_id, NOW - DAY)
    succeed(pg_session, user_id, NOW - DAY)
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

    def test_a_failed_window_comes_back_even_after_a_later_one_succeeds(
        self, pg_session, user_id
    ):
        """**这一条是那个洞的回归测试。**

        原来这里只断言 `last_successful_window_end is None` —— 那句是真的,
        但它证明不了失败的窗口会被重跑,而实际上它不会:往前切确实切出了
        周一,`claim_window` 的 `DO NOTHING` 又把它跳过去,然后周二成功,
        成功点越过周一,那一天从此消失。

        **验证方式必须和要验证的东西对上**:要断言的是"它出现在下一次的
        清单里",不是"它没被算成成功"。
        """
        monday, tuesday = NOW - 2 * DAY, NOW - DAY
        claim(pg_session, user_id, monday, tuesday)
        fail(pg_session, user_id, monday)

        claim(pg_session, user_id, tuesday, NOW)
        succeed(pg_session, user_id, tuesday)
        # 成功点已经越过周一了 —— 往前切那一遍再也切不到它
        assert last_successful_window_end(user_id, pg_session, job_name=JOB) == NOW

        windows = windows_to_run(user_id, pg_session, job_name=JOB, now=NOW)
        assert (monday, tuesday) in windows
        # 出现在清单里还不够 —— 洞的另一半在认领那一步:
        # 原来它会撞上已存在的行、DO NOTHING、被当成"已经跑过"跳过
        assert claim(pg_session, user_id, monday, tuesday) is True

    def test_other_users_windows_are_invisible(self, pg_session, user_id):
        claim(pg_session, user_id, NOW - DAY)
        finish_window(user_id, pg_session, job_name=JOB, window_start=NOW - DAY, status="succeeded")
        other = "99999999-9999-9999-9999-999999999999"
        assert last_successful_window_end(other, pg_session, job_name=JOB) is None


class TestRetry:
    """失败的窗口能不能真的被重跑。**这一组是新增的** ——
    原来整个仓库里没有一条测试碰过"认领一个 failed 的窗口"这件事,
    于是那个洞在库里躺了整整两个阶段。"""

    def test_a_failed_window_can_be_claimed_again(self, pg_session, user_id):
        start = NOW - DAY
        assert claim(pg_session, user_id, start) is True
        fail(pg_session, user_id, start)
        assert claim(pg_session, user_id, start) is True

    def test_retries_run_out(self, pg_session, user_id):
        """**无限重试比放弃更糟。** 一个永远失败的窗口会把后面每一天的
        名额都吃掉,而那时丢的就不是一天了。"""
        start = NOW - DAY
        for _ in range(MAX_ATTEMPTS):
            assert claim(pg_session, user_id, start) is True
            fail(pg_session, user_id, start)
        assert claim(pg_session, user_id, start) is False
        # 放弃了也不删、不改成 succeeded:查得到才知道丢了哪一天
        row = pg_session.execute(
            text("SELECT status, attempts, error FROM job_runs WHERE job_name = :j"), {"j": JOB}
        ).one()
        assert (row.status, row.attempts) == ("failed", MAX_ATTEMPTS)
        assert row.error == "LLM 超时"

    def test_attempts_counts_up(self, pg_session, user_id):
        start = NOW - DAY
        claim(pg_session, user_id, start)
        fail(pg_session, user_id, start)
        claim(pg_session, user_id, start)
        attempts = pg_session.execute(text("SELECT attempts FROM job_runs")).scalar_one()
        assert attempts == 2

    def test_a_fresh_running_window_is_not_stolen(self, pg_session, user_id):
        """刚开始跑的窗口不许被另一个进程抢走。"""
        start = NOW - DAY
        claim(pg_session, user_id, start, now=NOW)
        assert claim(pg_session, user_id, start, now=NOW + timedelta(minutes=30)) is False

    def test_a_running_window_from_six_hours_ago_is_dead(self, pg_session, user_id):
        """进程被 kill,行卡在 running。**那一天同样会丢** ——
        running 既不推进成功点,又(原来)不能被重新认领。"""
        start = NOW - DAY
        claim(pg_session, user_id, start, now=NOW)
        later = NOW + STALE_RUNNING_AFTER + timedelta(minutes=1)
        assert claim(pg_session, user_id, start, now=later) is True

    def test_a_stale_running_window_shows_up_in_the_list(self, pg_session, user_id):
        start = NOW - DAY
        claim(pg_session, user_id, start, now=NOW)
        later = NOW + STALE_RUNNING_AFTER + timedelta(minutes=1)
        assert retryable_windows(user_id, pg_session, job_name=JOB, now=later) == [
            (start, start + DAY)
        ]

    def test_retry_list_and_catchup_list_do_not_double_up(self, pg_session, user_id):
        """失败的窗口恰好还在成功点后面时,两份清单会指到同一个窗口。
        重复认领本身是安全的(第二次拿不到),但那会白跑一遍,
        而且日志里会出现看不懂的"已认领"。"""
        start = NOW - DAY
        claim(pg_session, user_id, start, NOW)
        fail(pg_session, user_id, start)
        windows = windows_to_run(user_id, pg_session, job_name=JOB, now=NOW)
        assert windows == [(start, NOW)]

    def test_a_used_up_window_stops_showing_up(self, pg_session, user_id):
        """三次之后它不再进清单 —— 否则往前追的进度会被它长期拖住。"""
        start = NOW - DAY
        for _ in range(MAX_ATTEMPTS):
            claim(pg_session, user_id, start)
            fail(pg_session, user_id, start)
        assert retryable_windows(user_id, pg_session, job_name=JOB, now=NOW) == []

    def test_other_users_failures_are_invisible(self, pg_session, user_id):
        start = NOW - DAY
        claim(pg_session, user_id, start)
        fail(pg_session, user_id, start)
        other = "99999999-9999-9999-9999-999999999999"
        assert retryable_windows(other, pg_session, job_name=JOB, now=NOW) == []
