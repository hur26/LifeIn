"""手机系统日历的归一化(06 §6.14)。

**日历数据源换人了。** 此前唯一的一条是企微日程,而企微整个退出了
([ADR-026](../../docs/04-tech-decisions.md#adr-026--企业微信整个退出消息面只剩-ilink-与邮件))。
换过来的不是一个凑合的替代品:系统日历里有企微同步过来的、飞书的、订阅的、
手动建的**全部**日程,而企微那条只看得见企微自己那一个。

## 这个模块只做"确定的那一半"

读日历、比对账户、过滤回环、切时间窗 —— 那些**全在设备端**,因为
`CalendarContract` 只有手机上有,而"这台手机上哪个账户是本人"服务端不知道。

这里做的是把设备报上来的那个形状变成 `NormalizedEvent`:
它只依赖字段含义,不依赖 Android 的接口形态。**接口变了要改的是 Kotlin,
不是这里** —— 这一条是从被删掉的那份企微日历适配器继承下来的,
那个文件当初就是这么分的,而分对了的证据是:企微没了,归一化这一半原样搬过来。

## 服务端不重算 trust

自己建的日程 `user_input`,别人邀请的 `external` —— 后者的标题和描述是别人
写的,**和一封邮件没有区别**([R3](../../docs/05-risks.md#r3--提示注入不可信的外部内容))。

判据是"organizer 是不是本人",而**本人是谁只有设备知道**(它拿
`Events.ORGANIZER` 和日历的 `OWNER_ACCOUNT` 比)。所以设备把结论放在
`self_organized` 里报上来,这里照用。

> **那是不是意味着设备可以撒谎、把别人的日程报成 `user_input`?**
> 是。但设备用的是**这个用户自己的采集凭据** —— 它本来就能凭空捏造任何一条
> 事件。这条链路的信任边界在凭据上,不在字段上(R11)。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from lifein.models.normalized import (
    EventKind,
    ExternalRef,
    IdentifierType,
    NormalizedEvent,
    Party,
    PartyRole,
    Trust,
)
from lifein.sources.base import IngestedEvent

log = logging.getLogger(__name__)

SOURCE = "calendar"
"""**沿用 `calendar`,不叫 `device_calendar`。**

06 §1.3 那张表里这一行本来就叫 `calendar`,而换的是供给方不是事件类别 ——
改名会让所有历史事件和新事件分成两拨,而它们是同一种东西。
"""

CANCELLED_PREFIX = "[已取消] "
"""取消的日程照样入库。

一个本来要去的会被取消,恰恰是当天最该知道的事之一 —— 不能因为"它不会
发生"就丢掉。前缀写在标题里,因为标题就是给人看的那一行。
"""

MAX_TITLE = 200
MAX_BODY = 2000
"""描述留多长。

日程描述里常常整段贴着会议纪要或者议程。**留全了会把摘要的素材预算吃光**,
而判断"这是什么会"用不到第两千个字。截断不是丢弃:`raw` 里那份是全的。
"""


def normalize(event: Mapping[str, Any], *, received_at: datetime) -> IngestedEvent:
    """把设备报上来的一条日程变成摄入事件。

    **解不出来不抛异常,落成 `normalize_error`。** 一条坏日程不该让同一批的
    另外九十九条一起退回去,而"它当时长什么样"要留在 `raw` 里 ——
    否则修好之后没法重跑(06 §1.4)。
    """
    raw = dict(event)
    event_id = str(event.get("event_id") or "").strip()

    if not event_id:
        # 没有 id 就没有去重键,重跑会不断产生重复事件
        return _unusable(raw, received_at, "日程缺 event_id,没有去重键")

    starts_at = _parse_time(event.get("starts_at"))
    if starts_at is None:
        return _unusable(
            raw, received_at, f"starts_at 缺失或解不开:{event.get('starts_at')!r}", event_id
        )

    title = str(event.get("title") or "").strip()[:MAX_TITLE] or "(无标题日程)"
    if event.get("cancelled"):
        title = CANCELLED_PREFIX + title

    # 设备说了算(见模块开头那段)
    trust = Trust.USER_INPUT if event.get("self_organized") else Trust.EXTERNAL

    normalized = NormalizedEvent(
        kind=EventKind.CALENDAR_EVENT,
        title=title,
        occurred_at=starts_at,
        external_ref=ExternalRef(source=SOURCE, external_id=event_id),
        trust=trust,
        confidence=1.0,
        parties=_parties(event),
        body=(str(event.get("description") or "").strip()[:MAX_BODY]) or None,
        location=(str(event.get("location") or "").strip()) or None,
    )
    return IngestedEvent(
        source=SOURCE,
        external_id=event_id,
        occurred_at=starts_at,
        trust=trust,
        raw=raw,
        normalized=normalized,
    )


def _unusable(
    raw: dict[str, Any], received_at: datetime, reason: str, event_id: str | None = None
) -> IngestedEvent:
    """解不开的那一条。**仍然入库** —— 见 `normalize` 的说明。

    `external_id` 退回一个带时间戳的占位:没有 id 的那种情况下,
    用固定字符串会让所有坏日程撞成同一行,于是只留得下第一条 ——
    而"当时报上来了几条坏的"是查这类问题的第一个数字。
    """
    return IngestedEvent(
        source=SOURCE,
        external_id=event_id or f"missing:{received_at.timestamp()}",
        occurred_at=received_at,
        trust=Trust.EXTERNAL,
        raw=raw,
        normalize_error=reason,
    )


def _parse_time(value: Any) -> datetime | None:
    """ISO 8601,**必须带时区**。

    不带时区的话服务器和手机在不同时区时,一场晚上八点的会会落到别的一天,
    而月度报表和每日摘要都按天切 —— 那种错在界面上完全看不出来。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _parties(event: Mapping[str, Any]) -> list[Party]:
    """组织者与参与者。

    标识类型是 `EMAIL`:系统日历里这两样都是邮箱地址(`Events.ORGANIZER`、
    `Attendees.ATTENDEE_EMAIL`)。**这一点比企微那版好** —— 邮箱能和邮件
    那条链路上的人对上,而企微 userid 只在企微里有意义。
    """
    parties: list[Party] = []

    organizer = str(event.get("organizer") or "").strip()
    if organizer:
        parties.append(
            Party(
                role=PartyRole.ORGANIZER,
                display_name=organizer,
                identifier=organizer,
                identifier_type=IdentifierType.EMAIL,
            )
        )

    for attendee in event.get("attendees") or []:
        name = str(attendee or "").strip()
        if not name or name == organizer:
            continue
        parties.append(
            Party(
                role=PartyRole.ATTENDEE,
                display_name=name,
                identifier=name,
                identifier_type=IdentifierType.EMAIL,
            )
        )
    return parties
