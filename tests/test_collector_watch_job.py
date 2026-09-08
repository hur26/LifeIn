"""采集器掉线告警的集成测试。

需要真实 PostgreSQL:被测的行为一半在 `channel_state` 里 ——
"已经告过警了"这件事记在那儿,而不重复打扰正是这个 job 最容易做坏的地方。

四条:掉线要告警、权限被收走也要告警、**同一次掉线只告一次**、
恢复之后重新掉线要能再告一次。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lifein.alerts import CollectingAlerter
from lifein.jobs.collector_watch import WatchDeps, run_once
from lifein.repos import collector

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
DEVICE = "pixel-7a"


def deps() -> WatchDeps:
    return WatchDeps(alerter=CollectingAlerter(), timeout_minutes=60)


def beat(session, user_id, *, minutes_ago: int, listener: bool = True) -> None:
    collector.record_heartbeat(
        user_id,
        session,
        device_id=DEVICE,
        now=NOW - timedelta(minutes=minutes_ago),
        listener_enabled=listener,
    )


def test_a_live_collector_says_nothing(pg_session, user_id):
    beat(pg_session, user_id, minutes_ago=10)
    d = deps()

    result = run_once(user_id, pg_session, deps=d, now=NOW)

    assert result.checked == 1
    assert result.alerted == []
    assert d.alerter.alerts == []


def test_silence_past_the_timeout_alerts(pg_session, user_id):
    beat(pg_session, user_id, minutes_ago=90)
    d = deps()

    result = run_once(user_id, pg_session, deps=d, now=NOW)

    assert result.alerted == [DEVICE]
    (title, detail) = d.alerter.alerts[0]
    assert title == "采集器掉线"
    # 告警正文要能直接看出该去做什么,不用再登服务器查一遍
    assert "90 分钟" in detail


def test_listener_switched_off_counts_as_offline(pg_session, user_id):
    """心跳一切正常,就是采不到东西 —— 这种更隐蔽,所以更要告。"""
    beat(pg_session, user_id, minutes_ago=1, listener=False)
    d = deps()

    result = run_once(user_id, pg_session, deps=d, now=NOW)

    assert result.alerted == [DEVICE]
    assert "通知监听权限" in d.alerter.alerts[0][1]


def test_the_same_outage_only_alerts_once(pg_session, user_id):
    """告警变成每五分钟一条之后,真出事的那次就被忽略了。"""
    beat(pg_session, user_id, minutes_ago=90)
    d = deps()

    run_once(user_id, pg_session, deps=d, now=NOW)
    second = run_once(user_id, pg_session, deps=d, now=NOW + timedelta(minutes=5))

    assert second.alerted == []
    assert second.still_down == 1
    assert len(d.alerter.alerts) == 1


def test_recovering_resets_so_the_next_outage_alerts_again(pg_session, user_id):
    beat(pg_session, user_id, minutes_ago=90)
    d = deps()
    run_once(user_id, pg_session, deps=d, now=NOW)

    # 修好了,心跳回来
    beat(pg_session, user_id, minutes_ago=0)
    recovered = run_once(user_id, pg_session, deps=d, now=NOW)
    assert recovered.recovered == [DEVICE]
    assert len(d.alerter.alerts) == 1  # 恢复只写日志,不再发一封

    # 又掉了 —— 这次要重新告
    later = NOW + timedelta(hours=3)
    again = run_once(user_id, pg_session, deps=d, now=later)
    assert again.alerted == [DEVICE]
    assert len(d.alerter.alerts) == 2


def test_a_device_that_never_reported_is_not_an_outage(pg_session, user_id):
    """签了凭据但 App 还没装 —— 那是"还没开始",不是"掉线了"。"""
    result = run_once(user_id, pg_session, deps=deps(), now=NOW)
    assert (result.checked, result.alerted) == (0, [])


def test_the_boundary_is_the_configured_timeout(pg_session, user_id):
    """判据是"离上次心跳多久",不是任何窗口 —— 所以补跑一次旧的没有意义。

    正好卡在超时那一刻不算掉线:一台严格每 60 分钟报一次的设备,
    不该每一轮都被判成掉线。
    """
    beat(pg_session, user_id, minutes_ago=60)
    assert run_once(user_id, pg_session, deps=deps(), now=NOW).alerted == []

    beat(pg_session, user_id, minutes_ago=61)
    assert run_once(user_id, pg_session, deps=deps(), now=NOW).alerted == [DEVICE]
