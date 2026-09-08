"""安卓采集器上报的通知 —— 架构 §9.1 里的**推送型**数据源,第一个。

前面所有数据源都是"我们去拉";这一个是"它送上门"。差别只在事件从哪来,
产出完全一样,都是 `IngestedEvent`。

**这一层不判断内容,只筛选和归一化。** 采集器不解析、不归类(架构 §8.1),
服务端这一步也不做语义判断 —— 谁在群里说了什么、要不要变成日程,
那是 planner agent 的事,它读 `raw_events`。

筛选的三道顺序写死(06 §6.4),顺序本身是安全机制:

    白名单(默认拒绝) → purpose 闸门(P1 只放消息) → 验证码正则 → 归一化

验证码那道放在最后,是因为前两道已经把绝大多数东西挡在外面了 ——
而**它挡的那一类,漏一条的代价是资金损失**(R10),所以它必须是
"就算前面全放行了也还在"的那一道。

**这个模块不碰数据库。** 白名单规则由调用方查好传进来,
所以整条筛选链在测试里不需要真库就能逐条验 —— 而它正是最该被逐条验的地方。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from lifein.models.normalized import (
    EventKind,
    ExternalRef,
    Flag,
    IdentifierType,
    NormalizedEvent,
    Party,
    PartyRole,
    Trust,
)
from lifein.repos.collector import PURPOSE_MESSAGE, WhitelistRule
from lifein.sources.base import IngestedEvent
from lifein.sources.verification_code import looks_like_verification_code

log = logging.getLogger(__name__)

SOURCE = "notification"
"""`raw_events.source`。**短信也用这个值**:架构 §2.1 的来源枚举里没有 `sms`,
而两者对下游是同一类东西(手机上展示过的一段文字)。
是通知还是短信记在 `raw.channel` 里,P2 记账要分开处理时按它分。"""

CHANNEL_NOTIFICATION = "notification"
CHANNEL_SMS = "sms"

OPEN_PURPOSES = frozenset({PURPOSE_MESSAGE})
"""P1 放行的 purpose。加 `transaction` 是 P2 的动作,**改这一行就是打开记账链路** ——
放在这里而不是散在判断里,是为了让那件事只有一个开关。"""

_AGGREGATED = re.compile(r"^\s*\[\s*\d+\s*条\s*\]")
"""系统把多条折叠成"[3 条] 张三: ……"。归一化留不下被折叠掉的那些,
所以要打标 —— 下游看到这条得知道自己看的是残缺的。"""

TITLE_FALLBACK_CHARS = 40


class DropReason:
    """丢弃原因。**和响应体里的键名是同一份**(06 §6.4),不要各写各的。"""

    NOT_WHITELISTED = "not_whitelisted"
    PHASE_NOT_OPEN = "phase_not_open"
    VERIFICATION_CODE = "verification_code"
    MALFORMED = "malformed"


@dataclass
class Screened:
    """一次上报筛完的结果。

    `events` 是要入库的,`dropped` 是**为什么没入库** —— 后者必须回给设备,
    否则采集端只能看到"发出去了",看不到"被收下了没有",
    而静默丢弃正是这条链路最该防的失效方式(R8)。
    """

    events: list[IngestedEvent] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


class NotificationAdapter:
    """把一次上报解成事件。架构 §9.1 的 `PushAdapter`。

    `device_id` 由**调用方按签名认出来的那个**传进来,不取 body 里的 ——
    body 是对方说了算的,而 `external_id` 要拼上它做去重键。
    """

    source = SOURCE

    def __init__(self, rules: Sequence[WhitelistRule], *, device_id: str) -> None:
        self._rules = [rule for rule in rules if rule.enabled]
        self._device_id = device_id

    def handle(self, payload: Mapping[str, Any]) -> list[IngestedEvent]:
        """`PushAdapter` 协议要的形状。丢弃原因要看 `screen()`。"""
        return self.screen(payload).events

    def screen(self, payload: Mapping[str, Any]) -> Screened:
        result = Screened()
        for item in payload.get("events") or []:
            self._screen_one(dict(item), result)
        return result

    def _screen_one(self, item: dict[str, Any], result: Screened) -> None:
        package_name = _text(item.get("source_app"))
        sender = _text(item.get("sender"))

        rule = self._match(package_name=package_name, sender=sender)
        if rule is None:
            # 默认拒绝。手机端已经过滤过一次,这里不假设那次是对的
            result.drop(DropReason.NOT_WHITELISTED)
            return

        if rule.purpose not in OPEN_PURPOSES:
            # 白名单可以提前配好,放行是另一回事 —— 记账链路 P2 才打开
            result.drop(DropReason.PHASE_NOT_OPEN)
            return

        title = _text(item.get("title"))
        body = _text(item.get("text"))
        if looks_like_verification_code(title, body):
            # 整条丢弃。日志只记条数不记原文 —— 记下来等于把刚拦住的东西
            # 写进另一个更少人看管的地方
            log.info("丢弃一条疑似验证码的通知,来源 %s", package_name or sender or "未知")
            result.drop(DropReason.VERIFICATION_CODE)
            return

        external_id = _text(item.get("external_id"))
        occurred_at = _parse_time(item.get("posted_at"))
        if not external_id or occurred_at is None or not (title or body):
            # 形状不对的**不入库**:没有 external_id 就没有去重键,重发一次
            # 就多一条;没有时间就落不进任何窗口。这两样缺一条都不是
            # "归一化失败"那一档,而是"这条根本没法处理"
            result.drop(DropReason.MALFORMED)
            return

        result.events.append(
            self._to_event(
                item,
                external_id=external_id,
                occurred_at=occurred_at,
                title=title,
                body=body,
                sender=sender,
            )
        )

    def _match(self, *, package_name: str, sender: str) -> WhitelistRule | None:
        for rule in self._rules:
            if rule.matches(package_name=package_name or None, sender=sender or None):
                return rule
        return None

    def _to_event(
        self,
        item: dict[str, Any],
        *,
        external_id: str,
        occurred_at: datetime,
        title: str,
        body: str,
        sender: str,
    ) -> IngestedEvent:
        display = title or body[:TITLE_FALLBACK_CHARS]
        # 标题和正文都看:折叠标记("[3 条] 张三: ……")落在哪一边由 ROM 决定,
        # 各家不一样。只看一边就会在换手机之后悄悄漏掉这个标
        aggregated = _AGGREGATED.search(title) or _AGGREGATED.search(body)
        flags = [Flag.AGGREGATED] if aggregated else []

        normalized = NormalizedEvent(
            kind=EventKind.MESSAGE,
            title=display,
            occurred_at=occurred_at,
            external_ref=ExternalRef(source=SOURCE, external_id=self._scoped(external_id)),
            # 通知一律不可信:群里任何一个人、任何一个商户都能构造它的内容(R3)
            trust=Trust.EXTERNAL,
            # 字段是设备原样送来的,归一化本身没有猜的成分。
            # 这个 1.0 说的是"解析没走样",不是"内容可信"
            confidence=1.0,
            parties=_parties(title=title, sender=sender),
            body=body or None,
            flags=flags,
        )
        return IngestedEvent(
            source=SOURCE,
            external_id=self._scoped(external_id),
            occurred_at=occurred_at,
            trust=Trust.EXTERNAL,
            # 原文进 raw:解析器改好之后要能按同一个键重跑(06 §1.4)。
            # 它也是通知留存期那件事作用的地方(R10)
            raw={
                "device_id": self._device_id,
                "channel": _text(item.get("channel")) or CHANNEL_NOTIFICATION,
                "source_app": _text(item.get("source_app")),
                "sender": sender,
                "title": title,
                "text": body,
                "posted_at": occurred_at.isoformat(),
            },
            normalized=normalized,
        )

    def _scoped(self, external_id: str) -> str:
        """去重键拼上设备 id。

        两台手机可能各自生成同一个通知 id(安卓的 key 只在本机唯一),
        不拼的话第二台上报的会被当成重复静默丢掉。
        """
        return f"{self._device_id}:{external_id}"


def _parties(*, title: str, sender: str) -> list[Party]:
    """谁发的。

    通知的标题就是群名或联系人名 —— 这是通知监听能拿到的全部身份信息,
    没有更结构化的东西可抽(ADR-010 的能力边界)。短信多一个号码,
    那个能当标识符用。
    """
    if sender:
        return [
            Party(
                role=PartyRole.FROM,
                display_name=sender,
                identifier=sender,
                identifier_type=IdentifierType.PHONE,
            )
        ]
    if title:
        return [Party(role=PartyRole.FROM, display_name=title)]
    return []


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _parse_time(value: Any) -> datetime | None:
    """解 `posted_at`。**必须带时区** —— 没有时区的时间戳会在窗口计算上
    悄悄错整整八小时,而错进日历的日程比没有这个功能糟得多。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
