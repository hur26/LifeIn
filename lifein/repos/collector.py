"""采集端那两张表:`collector_whitelist` 与 `collector_heartbeat`。

放一个模块里,因为它们服务同一件事 —— **手机上那个采集器现在是什么状态、
它被允许送什么进来**。分两个模块会让"采集器怎么样了"这个问题要翻两处。

两张表各自挡一类失效:

**白名单挡越权。** 默认拒绝,不在表里的一律不入库
([R10](../../docs/05-risks.md#r10--手机端采集器的越权读取))。
手机端已经过滤过一次,这是第二道 —— 不假设手机端规则永远正确。

**心跳挡静默。** 采集器掉线时系统表现得一切正常,只是提醒和账目悄悄变少
([架构 §8.6](../../docs/02-architecture.md#86-已知能力边界))。
`last_seen_at` 由**服务端**写,不采信设备时钟:那个值是告警的判据,
而告警最不该依赖被告警对象报上来的东西。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

MATCH_PACKAGE = "package_name"
MATCH_SMS_SENDER = "sms_sender"

PURPOSE_MESSAGE = "message"
PURPOSE_TRANSACTION = "transaction"
"""P2 才打开的那一类。P1 的闸门在 `sources/notification.py`,不在这张表 ——
表里可以提前配好,放不放行是另一回事(06 §6.4 第 2 步)。"""


@dataclass(frozen=True)
class WhitelistRule:
    id: int
    match_type: str
    pattern: str
    purpose: str
    enabled: bool
    phase: str

    def matches(self, *, package_name: str | None, sender: str | None) -> bool:
        """这条规则放不放行某条上报。

        包名**全等**,短信发件人**前缀**:银行短信的号码是号段(95588、
        1069xxxx),写死全等等于每换一个下发通道就漏一批。包名没有这个问题,
        全等更安全 —— 前缀匹配会让 `com.tencent.mm` 顺带放行
        `com.tencent.mm.fake`。
        """
        if self.match_type == MATCH_PACKAGE:
            return bool(package_name) and package_name == self.pattern
        if self.match_type == MATCH_SMS_SENDER:
            return bool(sender) and sender.startswith(self.pattern)
        return False


@dataclass(frozen=True)
class Heartbeat:
    device_id: str
    last_seen_at: datetime
    app_version: str | None
    android_version: str | None
    listener_enabled: bool

    def is_stale(self, *, cutoff: datetime) -> bool:
        return self.last_seen_at < cutoff

    def needs_alert(self, *, cutoff: datetime) -> bool:
        """要不要告警。

        **权限被收走也算掉线。** 进程还在、心跳照常,但通知监听读不到东西 ——
        后果和掉线一样,表现却更隐蔽。只看 `last_seen_at` 会漏掉这一类。
        """
        return self.is_stale(cutoff=cutoff) or not self.listener_enabled


_LIST_RULES = text("""
    SELECT id, match_type, pattern, purpose, enabled, phase
      FROM collector_whitelist
     WHERE user_id = :user_id
       AND (:enabled_only = false OR enabled = true)
     ORDER BY created_at
""")

_ADD_RULE = text("""
    INSERT INTO collector_whitelist (user_id, match_type, pattern, purpose, phase)
    VALUES (:user_id, :match_type, :pattern, :purpose, :phase)
    ON CONFLICT (user_id, match_type, pattern)
    DO UPDATE SET enabled = true, purpose = EXCLUDED.purpose, phase = EXCLUDED.phase
    RETURNING id, match_type, pattern, purpose, enabled, phase
""")

_SET_ENABLED = text("""
    UPDATE collector_whitelist
       SET enabled = :enabled
     WHERE user_id = :user_id AND id = :rule_id
""")

_UPSERT_HEARTBEAT = text("""
    INSERT INTO collector_heartbeat
        (user_id, device_id, last_seen_at, app_version, android_version, listener_enabled)
    VALUES
        (:user_id, :device_id, :now, :app_version, :android_version, :listener_enabled)
    ON CONFLICT (user_id, device_id)
    DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at,
                  app_version = EXCLUDED.app_version,
                  android_version = EXCLUDED.android_version,
                  listener_enabled = EXCLUDED.listener_enabled
""")

_LIST_HEARTBEATS = text("""
    SELECT device_id, last_seen_at, app_version, android_version, listener_enabled
      FROM collector_heartbeat
     WHERE user_id = :user_id
     ORDER BY device_id
""")


def list_whitelist(
    user_id: str, session: Session, *, enabled_only: bool = False
) -> list[WhitelistRule]:
    """列白名单。停用的默认也列出来 —— 面板要能看见"曾经放行过谁"。"""
    rows = session.execute(_LIST_RULES, {"user_id": user_id, "enabled_only": enabled_only}).all()
    return [
        WhitelistRule(
            id=row.id,
            match_type=row.match_type,
            pattern=row.pattern,
            purpose=row.purpose,
            enabled=row.enabled,
            phase=row.phase,
        )
        for row in rows
    ]


def add_whitelist(
    user_id: str,
    session: Session,
    *,
    match_type: str,
    pattern: str,
    purpose: str,
    phase: str,
) -> WhitelistRule:
    """加一条来源。已经有同样的 (match_type, pattern) 就重新启用它。

    重新启用而不是报错:用户点"加回微信"时,他要的是结果,
    不是被告知"这条以前加过"。
    """
    row = session.execute(
        _ADD_RULE,
        {
            "user_id": user_id,
            "match_type": match_type,
            "pattern": pattern.strip(),
            "purpose": purpose,
            "phase": phase,
        },
    ).one()
    return WhitelistRule(
        id=row.id,
        match_type=row.match_type,
        pattern=row.pattern,
        purpose=row.purpose,
        enabled=row.enabled,
        phase=row.phase,
    )


def set_whitelist_enabled(user_id: str, session: Session, *, rule_id: int, enabled: bool) -> bool:
    """开关一条。**没有删除** —— 停用即不放行,而留着那一行能回答
    "曾经放行过谁",排查越权读取时那是唯一的线索(06 §6.9)。"""
    return session.execute(
        _SET_ENABLED, {"user_id": user_id, "rule_id": rule_id, "enabled": enabled}
    ).rowcount > 0


def record_heartbeat(
    user_id: str,
    session: Session,
    *,
    device_id: str,
    now: datetime,
    app_version: str | None = None,
    android_version: str | None = None,
    listener_enabled: bool = True,
) -> None:
    """记一次心跳。`now` 由调用方给的是**服务端时间**,不是设备报上来的。"""
    session.execute(
        _UPSERT_HEARTBEAT,
        {
            "user_id": user_id,
            "device_id": device_id,
            "now": now,
            "app_version": app_version,
            "android_version": android_version,
            "listener_enabled": listener_enabled,
        },
    )


def list_heartbeats(user_id: str, session: Session) -> list[Heartbeat]:
    """所有登记过的采集设备。掉线告警与状态面板都读它。"""
    rows = session.execute(_LIST_HEARTBEATS, {"user_id": user_id}).all()
    return [
        Heartbeat(
            device_id=row.device_id,
            last_seen_at=row.last_seen_at,
            app_version=row.app_version,
            android_version=row.android_version,
            listener_enabled=row.listener_enabled,
        )
        for row in rows
    ]
