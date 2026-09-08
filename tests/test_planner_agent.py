"""日程与待办提取 agent 的测试。不需要数据库。

这个 agent 的产出会变成日历里的东西,所以测的重点不是"提得全不全",
而是**什么东西不许直接落地**:

- 日程一律走待确认(03 的验收标准是"误报进日历的条数为 0")
- 没有时区的时间当成解析失败,不拿用户时区去补
- 已经过去的时间不建
- 指不回素材的一律丢
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from lifein.agents.planner import (
    TODO_DIRECT_MIN,
    ItemKind,
    PlannerFailed,
    PlannerInput,
    Route,
    build_blocks,
    extract,
    trust_of,
)
from lifein.llm.client import LLMClient
from lifein.models.normalized import (
    EventKind,
    ExternalRef,
    NormalizedEvent,
    Party,
    PartyRole,
    Trust,
)
from lifein.repos.raw_events import StoredEvent

NOW = datetime(2026, 9, 8, 9, tzinfo=UTC)
FUTURE = "2026-09-09T15:00:00+08:00"
PAST = "2026-09-01T15:00:00+08:00"


def stored(event_id: int = 1, external_id: str = "m1", *, trust=Trust.EXTERNAL) -> StoredEvent:
    return StoredEvent(
        event_id=event_id,
        event=NormalizedEvent(
            kind=EventKind.MESSAGE,
            title="项目周会",
            occurred_at=NOW - timedelta(hours=2),
            external_ref=ExternalRef(source="email", external_id=external_id),
            trust=trust,
            confidence=1.0,
            body="周三下午三点开个会,顺便帮我带个充电器。",
            parties=[Party(role=PartyRole.FROM, display_name="张三")],
        ),
    )


def llm_returning(payload) -> LLMClient:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    body = {
        "model": "m",
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    return LLMClient(
        base_url="https://llm.example.com/v1",
        api_key="k",
        model="m",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
        ),
        sleep=lambda _s: None,
    )


def run(items, events=None):
    return extract(
        PlannerInput(events=[stored()] if events is None else events, now=NOW),
        llm=llm_returning({"items": items}),
    )


def item(**overrides) -> dict:
    base = {"kind": "todo", "title": "帮张三带个充电器", "refs": ["m1"], "confidence": 0.9}
    base.update(overrides)
    return base


def test_confident_todo_is_created_directly():
    result = run([item()])
    [extracted] = result.output.items

    assert extracted.kind is ItemKind.TODO
    assert extracted.route is Route.DIRECT
    assert extracted.provenance == [1]


def test_low_confidence_todo_goes_to_the_queue():
    result = run([item(confidence=TODO_DIRECT_MIN - 0.1)])
    [extracted] = result.output.items

    assert extracted.route is Route.PENDING
    assert extracted.reason == "low_confidence"


def test_every_schedule_goes_to_the_queue_however_confident():
    """日程一律走待确认。

    03 的验收标准是"误报进日历的条数为 0",不是"低于多少" ——
    一个 0 指标没法靠置信度阈值保证,只能靠人点一下。
    """
    result = run([item(kind="schedule", starts_at=FUTURE, confidence=0.99)])
    [extracted] = result.output.items

    assert extracted.kind is ItemKind.SCHEDULE
    assert extracted.route is Route.PENDING
    assert extracted.reason == "check_failed"


def test_a_time_makes_it_a_schedule_regardless_of_what_the_model_said():
    # 类型由"解析出时间没有"决定,不由模型自称的 kind 决定 ——
    # 模型说 todo 却给了时间时,按有时间处理才不会漏掉一场会
    result = run([item(kind="todo", starts_at=FUTURE)])
    assert result.output.items[0].kind is ItemKind.SCHEDULE


def test_naive_time_is_treated_as_no_time_not_as_local_time():
    """无时区的时间当成解析失败,不拿用户时区去补。

    错了八小时的日程比一条没有时间的待办糟得多:后者你看一眼就知道要自己排,
    前者会让你准时错过。
    """
    result = run([item(kind="schedule", starts_at="2026-09-09T15:00:00")])
    [extracted] = result.output.items

    assert extracted.kind is ItemKind.TODO
    assert extracted.starts_at is None


def test_unparseable_time_degrades_to_a_todo():
    result = run([item(kind="schedule", starts_at="下周三下午")])
    assert result.output.items[0].kind is ItemKind.TODO


def test_past_events_are_not_created():
    # "上周三开会"是在描述已发生的事,建出来只会变成一条过期提醒
    result = run([item(kind="schedule", starts_at=PAST)])
    assert result.output.items == []
    assert result.output.dropped_past == 1


def test_item_without_refs_is_dropped():
    result = run([item(refs=[])])
    assert result.output.items == []
    assert result.output.dropped_ungrounded == 1


def test_item_referencing_an_unknown_event_is_dropped():
    result = run([item(refs=["不存在的信"])])
    assert result.output.items == []
    assert result.output.dropped_ungrounded == 1


def test_refs_are_matched_after_normalization():
    events = [stored(7, "<abc@mail.qq.com>")]
    result = run([item(refs=["ABC@mail.qq.com"])], events=events)
    assert result.output.items[0].provenance == [7]


def test_overlong_title_is_dropped():
    result = run([item(title="会" * 200)])
    assert result.output.items == []
    assert result.output.dropped_ungrounded == 1


def test_no_events_means_no_model_call():
    class Boom:
        @staticmethod
        def chat(_messages):
            raise AssertionError("不该调模型")

    result = extract(PlannerInput(events=[], now=NOW), llm=Boom())
    assert result.output.items == []
    assert result.prompt_tokens is None


def test_broken_json_fails_loudly():
    with pytest.raises(PlannerFailed):
        extract(PlannerInput(events=[stored()], now=NOW), llm=llm_returning("这不是 JSON"))


def test_blocks_carry_the_anchor_time():
    """相对时间的锚点是规则给的,不让模型猜"今天几号"。"""
    block = build_blocks([stored()])[0]
    assert block.fields["时间"] == (NOW - timedelta(hours=2)).isoformat()
    assert block.fields["发件人"] == "张三"


def test_trust_takes_the_least_trustworthy_source():
    assert trust_of([stored(1, trust=Trust.USER_INPUT)]) is Trust.USER_INPUT
    assert (
        trust_of([stored(1, trust=Trust.USER_INPUT), stored(2, "m2", trust=Trust.EXTERNAL)])
        is Trust.EXTERNAL
    )
    assert trust_of([]) is Trust.EXTERNAL
