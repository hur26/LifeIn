"""企业微信日程采集与归一化。

06 §1.3 那一行:

    calendar | calendar_event | summary | start_time | organizer + attendees
             | description | 自建 user_input,他人邀请 external

**这个文件分成确定的和不确定的两半,刻意分开:**

`normalize_schedule` 只依赖日程对象的**字段含义**,不依赖接口形态 ——
它现在就能写全并测透。`WecomCalendarAdapter` 依赖接口的路径与分页参数,
那部分只能对着真实接口校,标记在下面的常量上,也记在 07 里。
接口若与预期不符,要改的只有那几行,归一化不用动。

**trust 由谁建的决定。** 自己建的日程是 `user_input`,别人拉你进的是
`external` —— 后者的标题和描述是别人写的,和一封邮件没有区别。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lifein.channels.wecom_client import WecomClient
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

# ---- 待实测确认(见 docs/07-config.md §2.4)----
# 路径与分页参数只能对着真实接口校。校完改这里,归一化不用动。
LIST_PATH = "/oa/schedule/get_by_calendar"
PAGE_SIZE = 100
# ---------------------------------------------

CANCELLED_PREFIX = "[已取消] "
"""取消的日程照样入库。

一个本来要去的会被取消,恰恰是当天最该知道的事之一 —— 不能因为"它不会
发生"就丢掉。前缀写在标题里,因为标题就是给人看的那一行。
"""


@dataclass(frozen=True)
class CalendarConfig:
    cal_id: str
    owner_wecom_userid: str
    """本人的企微 userid。用来判断一条日程是自己建的还是别人拉的。"""


def _to_datetime(value: Any) -> datetime | None:
    """企微给的是秒级时间戳。

    值得多写一行的地方:毫秒时间戳会被当成公元 5 万年,而那种错误在摘要里
    表现为"这条日程莫名其妙不见了",查起来很费劲。所以量级不对就当没有。
    """
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if not (0 < seconds < 4_102_444_800):  # 上限约 2100 年
        return None
    return datetime.fromtimestamp(seconds, tz=UTC)


def _parties(schedule: Mapping[str, Any]) -> list[Party]:
    parties: list[Party] = []
    organizer = schedule.get("organizer")
    if organizer:
        parties.append(
            Party(
                role=PartyRole.ORGANIZER,
                display_name=str(organizer),
                identifier=str(organizer),
                identifier_type=IdentifierType.WECOM_USERID,
            )
        )
    for attendee in schedule.get("attendees") or []:
        userid = attendee.get("userid") if isinstance(attendee, Mapping) else None
        if not userid:
            continue
        parties.append(
            Party(
                role=PartyRole.ATTENDEE,
                display_name=str(userid),
                identifier=str(userid),
                identifier_type=IdentifierType.WECOM_USERID,
            )
        )
    return parties


def normalize_schedule(
    schedule: Mapping[str, Any],
    *,
    config: CalendarConfig,
    received_at: datetime,
) -> IngestedEvent:
    """把一个日程对象变成一条摄入事件。"""
    raw = dict(schedule)
    schedule_id = str(schedule.get("schedule_id") or "").strip()

    if not schedule_id:
        # 没有 id 就没有去重键,重跑会不断产生重复事件。必填字段缺失那一档
        return IngestedEvent(
            source=SOURCE,
            external_id=f"missing:{received_at.timestamp()}",
            occurred_at=received_at,
            trust=Trust.EXTERNAL,
            raw=raw,
            normalize_error="日程缺 schedule_id,没有去重键",
        )

    start = _to_datetime(schedule.get("start_time"))
    if start is None:
        return IngestedEvent(
            source=SOURCE,
            external_id=schedule_id,
            occurred_at=received_at,
            trust=Trust.EXTERNAL,
            raw=raw,
            normalize_error=f"start_time 缺失或不合理:{schedule.get('start_time')!r}",
        )

    organizer = str(schedule.get("organizer") or "")
    trust = Trust.USER_INPUT if organizer == config.owner_wecom_userid else Trust.EXTERNAL

    summary = str(schedule.get("summary") or "").strip() or "(无标题日程)"
    if int(schedule.get("status", 1)) == 0:
        summary = CANCELLED_PREFIX + summary

    normalized = NormalizedEvent(
        kind=EventKind.CALENDAR_EVENT,
        title=summary,
        occurred_at=start,
        external_ref=ExternalRef(source=SOURCE, external_id=schedule_id),
        trust=trust,
        confidence=1.0,
        parties=_parties(schedule),
        body=str(schedule.get("description") or "") or None,
        location=str(schedule.get("location") or "") or None,
    )
    return IngestedEvent(
        source=SOURCE,
        external_id=schedule_id,
        occurred_at=start,
        trust=trust,
        raw=raw,
        normalized=normalized,
    )


class WecomCalendarAdapter:
    source = SOURCE

    def __init__(self, client: WecomClient, config: CalendarConfig) -> None:
        self._client = client
        self._config = config

    def fetch(self, since: datetime) -> Iterator[IngestedEvent]:
        offset = 0
        while True:
            data = self._client.post(
                LIST_PATH,
                {
                    "cal_id": self._config.cal_id,
                    "offset": offset,
                    "limit": PAGE_SIZE,
                },
            )
            schedules: Sequence[Mapping[str, Any]] = data.get("schedule_list") or []
            if not schedules:
                return

            received_at = datetime.now(UTC)
            for schedule in schedules:
                event = normalize_schedule(schedule, config=self._config, received_at=received_at)
                # since 之前的照样产出:去重靠唯一键,而适配器保持无状态(base.py)。
                # 但太老的没必要往下送,免得每天把整本日历重算一遍
                if not event.failed and event.occurred_at < since:
                    continue
                yield event

            if len(schedules) < PAGE_SIZE:
                return
            offset += PAGE_SIZE
