"""企微日程归一化的测试。

归一化只依赖字段含义,不依赖接口形态,所以现在就能测透 —— 接口那半边
(路径、分页)标了待实测,改它不该影响这些用例。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from lifein.channels.wecom_client import WecomClient
from lifein.models.normalized import EventKind, PartyRole, Trust
from lifein.sources.calendar_source import (
    CANCELLED_PREFIX,
    PAGE_SIZE,
    CalendarConfig,
    WecomCalendarAdapter,
    normalize_schedule,
)

RECEIVED = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
CONFIG = CalendarConfig(cal_id="cal-1", owner_wecom_userid="BaiYang")

# 2026-09-07 07:00:00 UTC
START_TS = 1788764400


def schedule(**overrides) -> dict:
    base = {
        "schedule_id": "sch-1",
        "organizer": "BaiYang",
        "attendees": [{"userid": "Lee"}, {"userid": "Wang"}],
        "summary": "季度评审",
        "description": "带上上季度的数据",
        "location": "三楼会议室",
        "start_time": START_TS,
        "status": 1,
    }
    return {**base, **overrides}


def norm(**overrides):
    return normalize_schedule(schedule(**overrides), config=CONFIG, received_at=RECEIVED)


def test_normal_schedule():
    e = norm()
    n = e.normalized
    assert n.kind is EventKind.CALENDAR_EVENT
    assert n.title == "季度评审"
    assert n.location == "三楼会议室"
    assert n.occurred_at == datetime.fromtimestamp(START_TS, tz=UTC)


def test_own_schedule_is_user_input():
    assert norm(organizer="BaiYang").normalized.trust is Trust.USER_INPUT


def test_someone_elses_invite_is_external():
    """别人拉你进的日程,标题和描述是别人写的,和一封邮件没有区别。"""
    assert norm(organizer="Lee").normalized.trust is Trust.EXTERNAL


def test_organizer_and_attendees_get_distinct_roles():
    parties = norm().normalized.parties
    assert parties[0].role is PartyRole.ORGANIZER
    assert [p.identifier for p in parties if p.role is PartyRole.ATTENDEE] == ["Lee", "Wang"]


def test_cancelled_schedule_is_kept_and_marked():
    # 一个本来要去的会被取消,恰恰是当天最该知道的事之一
    e = norm(status=0)
    assert e.failed is False
    assert e.normalized.title.startswith(CANCELLED_PREFIX)


def test_missing_schedule_id_is_a_normalize_error():
    # 没有 id 就没有去重键,重跑会不断产生重复事件
    e = norm(schedule_id="")
    assert e.failed is True
    assert "schedule_id" in e.normalize_error


@pytest.mark.parametrize("bad", [None, "", "下午三点", 0, -1])
def test_bad_start_time_is_a_normalize_error(bad):
    assert norm(start_time=bad).failed is True


def test_millisecond_timestamp_is_rejected_not_silently_wrong():
    """毫秒时间戳会被当成公元 5 万年,在摘要里表现为"这条日程莫名其妙不见了"。"""
    e = norm(start_time=START_TS * 1000)
    assert e.failed is True


def test_empty_summary_still_produces_a_title():
    assert norm(summary="").normalized.title == "(无标题日程)"


def test_raw_is_always_kept():
    e = norm(schedule_id="")
    assert e.raw["summary"] == "季度评审"  # 修好解析器后能重跑


class TestAdapter:
    def build(self, pages: list[list[dict]]) -> tuple[WecomCalendarAdapter, list[dict]]:
        sent: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/gettoken"):
                return httpx.Response(
                    200, json={"errcode": 0, "access_token": "t", "expires_in": 7200}
                )
            body = json.loads(request.content)
            sent.append(body)
            index = body["offset"] // PAGE_SIZE
            page = pages[index] if index < len(pages) else []
            return httpx.Response(200, json={"errcode": 0, "schedule_list": page})

        client = WecomClient(
            corp_id="c",
            secret="s",
            agent_id="1",
            base_url="https://qyapi.example.com/cgi-bin",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        return WecomCalendarAdapter(client, CONFIG), sent

    def test_single_page(self):
        adapter, sent = self.build([[schedule()]])
        events = list(adapter.fetch(datetime(2026, 9, 1, tzinfo=UTC)))
        assert [e.external_id for e in events] == ["sch-1"]
        assert sent[0]["cal_id"] == "cal-1"

    def test_paging_stops_on_a_short_page(self):
        full = [schedule(schedule_id=f"s{i}") for i in range(PAGE_SIZE)]
        adapter, sent = self.build([full, [schedule(schedule_id="last")]])
        events = list(adapter.fetch(datetime(2026, 9, 1, tzinfo=UTC)))
        assert len(events) == PAGE_SIZE + 1
        assert len(sent) == 2  # 第二页不满,不再请求第三页

    def test_old_schedules_are_skipped(self):
        # 不把整本日历每天重算一遍
        old = schedule(schedule_id="old", start_time=START_TS - 86400 * 30)
        adapter, _ = self.build([[old, schedule()]])
        events = list(adapter.fetch(datetime(2026, 9, 1, tzinfo=UTC)))
        assert [e.external_id for e in events] == ["sch-1"]

    def test_failed_events_are_never_skipped_by_the_time_filter(self):
        """归一化失败的事件必须送出去 —— 它要告警,而不是被当成"太老"过滤掉。"""
        adapter, _ = self.build([[schedule(schedule_id="")]])
        events = list(adapter.fetch(datetime(2026, 9, 1, tzinfo=UTC)))
        assert len(events) == 1
        assert events[0].failed is True
