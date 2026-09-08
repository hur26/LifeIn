"""采集器掉线告警。

**这个项目最危险的失效方式不是崩溃,是安静**(`alerts.py` 开头那句)。
采集器静默掉线时系统表现得一切正常,只是群消息摘要变短、提醒变少 ——
不告警的话半个月都发现不了([架构 §8.6](../../docs/02-architecture.md#86-已知能力边界))。
P1 的验收标准里因此有一条:**掉线能在 1 小时内告警**。

两种情况都算掉线,后一种更隐蔽:

| 情况 | 表现 |
| --- | --- |
| 超过 `COLLECTOR_HEARTBEAT_TIMEOUT_M` 没心跳 | 进程被 ROM 杀了、手机关机、网络断了 |
| 心跳正常但 `listener_enabled=false` | **通知监听权限被系统收走** —— 心跳一切正常,就是采不到东西 |

**每次掉线只告警一次。** 告警变成每五分钟一条之后,真出事的那次就被忽略了
(R8 那句"告警变成噪音")。已经告过警这件事记在 `channel_state` 里 ——
那张表存的都是"丢了重来一次就好"的状态,丢了最多多发一条告警,正好合适。

**从没上报过的设备不算掉线。** 签发了凭据但 App 还没装,列表里根本没有那一行;
有那一行就说明它至少活过一次,那时候安静下去才是问题。

时间账要说清:超时 60 分钟 + 扫描间隔 5 分钟 = **最晚 65 分钟发出**。
要严格卡进一小时,把 `COLLECTOR_HEARTBEAT_TIMEOUT_M` 调到 55。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from lifein.alerts import Alerter
from lifein.repos import channel_state, collector

log = logging.getLogger(__name__)

JOB_NAME = "collector_watch"
CHANNEL = "collector"
ALERTED_KEY = "offline_alerted"


@dataclass(frozen=True)
class WatchDeps:
    alerter: Alerter
    timeout_minutes: int = 60


@dataclass
class WatchResult:
    checked: int = 0
    alerted: list[str] = field(default_factory=list)
    """这一轮新发出去的告警(设备 id)。"""

    recovered: list[str] = field(default_factory=list)
    still_down: int = 0
    """还在掉线但已经告过警的。**不重复打扰**,只留个数字给日志。"""


def run_once(user_id: str, session: Session, *, deps: WatchDeps, now: datetime) -> WatchResult:
    """扫一遍这个用户的采集设备。

    **不认领窗口**(不走 `job_runs`):补跑一次两小时前的掉线检查没有意义 ——
    掉没掉线看的是"现在离上次心跳多久",那是个当下的事实,不是一个窗口。
    """
    result = WatchResult()
    cutoff = now - timedelta(minutes=deps.timeout_minutes)

    for beat in collector.list_heartbeats(user_id, session):
        result.checked += 1
        already = channel_state.get_state(
            user_id, session, channel=CHANNEL, key=_key(beat.device_id)
        )

        if beat.needs_alert(cutoff=cutoff):
            if already:
                result.still_down += 1
                continue
            deps.alerter.alert("采集器掉线", _describe(beat, now=now))
            channel_state.set_state(
                user_id,
                session,
                channel=CHANNEL,
                key=_key(beat.device_id),
                value=now.isoformat(),
            )
            result.alerted.append(beat.device_id)
            continue

        if already:
            # 恢复了只写日志不发告警:你多半正是刚去修好它的那个人,
            # 一条"它好了"的邮件只会让告警邮箱更难看
            log.info("采集器 %s 恢复上报", beat.device_id)
            channel_state.clear_state(
                user_id, session, channel=CHANNEL, key=_key(beat.device_id)
            )
            result.recovered.append(beat.device_id)

    return result


def _key(device_id: str) -> str:
    return f"{ALERTED_KEY}:{device_id}"


def _describe(beat: collector.Heartbeat, *, now: datetime) -> str:
    """告警正文要能直接看出该去做什么,不用再登服务器查一遍。"""
    if not beat.listener_enabled:
        return (
            f"设备 {beat.device_id} 的通知监听权限已关闭 —— 心跳还在,但采不到任何东西。"
            "去手机的通知使用权设置里重新打开"
        )
    silent = now - beat.last_seen_at
    minutes = int(silent.total_seconds() // 60)
    return (
        f"设备 {beat.device_id} 已经 {minutes} 分钟没有心跳"
        f"(最后一次 {beat.last_seen_at:%Y-%m-%d %H:%M})。"
        "多半是被 ROM 杀了后台或者关机了,去看看自启动白名单"
    )
