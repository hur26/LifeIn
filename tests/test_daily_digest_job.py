"""每日摘要 job 的集成测试 —— P0 完整链路。

需要真实 PostgreSQL(见 conftest.py)。

这一组测的全是**失败被关在哪里**:一个数据源挂了会不会拖垮另一个、
摘要生成失败会不会发出半条、推送失败还查不查得到。正常路径只占一个用例,
因为正常路径本来就不会出问题。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from lifein.alerts import CollectingAlerter
from lifein.channels.base import Card, Delivery
from lifein.jobs.daily_digest import JOB_NAME, DigestDeps, run_once
from lifein.llm.client import LLMClient
from lifein.models.normalized import EventKind, ExternalRef, NormalizedEvent, Trust
from lifein.repos import job_runs
from lifein.sources.base import IngestedEvent

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)
DAY = timedelta(days=1)


class FakeAdapter:
    def __init__(self, source: str, events=None, boom: Exception | None = None) -> None:
        self.source = source
        self._events = events or []
        self._boom = boom

    def fetch(self, since):
        if self._boom:
            raise self._boom
        return list(self._events)


class FakeChannel:
    name = "wecom"

    def __init__(self, boom: Exception | None = None) -> None:
        self.sent: list[Card] = []
        self._boom = boom

    def send(self, user_id, card):
        if self._boom:
            raise self._boom
        self.sent.append(card)
        return Delivery(channel=self.name, delivery_id="msg-1")


def event(external_id: str, *, at: datetime | None = None) -> IngestedEvent:
    when = at or (NOW - timedelta(hours=2))
    return IngestedEvent(
        source="email",
        external_id=external_id,
        occurred_at=when,
        trust=Trust.EXTERNAL,
        raw={"subject": "报销单"},
        normalized=NormalizedEvent(
            kind=EventKind.MESSAGE,
            title="报销单已通过",
            occurred_at=when,
            external_ref=ExternalRef(source="email", external_id=external_id),
            trust=Trust.EXTERNAL,
            confidence=1.0,
            body="金额 1280 元",
        ),
    )


def failed_event(external_id: str) -> IngestedEvent:
    return IngestedEvent(
        source="email",
        external_id=external_id,
        occurred_at=NOW - timedelta(hours=2),
        trust=Trust.EXTERNAL,
        raw={"subject": "坏的"},
        normalize_error="缺 Date 头",
    )


def fake_llm(payload=None, status: int = 200) -> LLMClient:
    text_out = json.dumps(
        payload
        or {"summary": "今天有 1 件要紧事。", "items": [{"category": "todo", "text": "回邮件"}]},
        ensure_ascii=False,
    )
    body = {
        "model": "m",
        "choices": [{"message": {"content": text_out}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }
    return LLMClient(
        base_url="https://llm.example.com/v1",
        api_key="k",
        model="m",
        max_retries=0,
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body))
        ),
        sleep=lambda _s: None,
    )


def deps(*, adapters=None, llm=None, channel=None, alerter=None) -> tuple[DigestDeps, dict]:
    channel = channel or FakeChannel()
    alerter = alerter or CollectingAlerter()
    return (
        DigestDeps(
            adapters=adapters if adapters is not None else [FakeAdapter("email", [event("m1")])],
            llm=llm or fake_llm(),
            channel=channel,
            alerter=alerter,
        ),
        {"channel": channel, "alerter": alerter},
    )


def test_happy_path(pg_session, user_id):
    d, handles = deps()
    [result] = run_once(user_id, pg_session, deps=d, now=NOW)

    assert result.inserted == 1
    assert result.pushed is True
    assert result.error is None
    assert handles["channel"].sent[0].title == "9 月 7 日摘要"

    # 三张表都写到了
    assert pg_session.execute(text("SELECT count(*) FROM raw_events")).scalar_one() == 1
    assert pg_session.execute(text("SELECT count(*) FROM push_log")).scalar_one() == 1
    assert pg_session.execute(text("SELECT count(*) FROM tool_calls")).scalar_one() == 1
    assert (
        pg_session.execute(
            text("SELECT status FROM job_runs WHERE job_name = :j"), {"j": JOB_NAME}
        ).scalar_one()
        == "succeeded"
    )


def test_one_broken_source_does_not_stop_the_other(pg_session, user_id):
    """邮箱认证失效不该让日历也采不到,更不该让今天没有摘要。"""
    d, handles = deps(
        adapters=[
            FakeAdapter("email", boom=RuntimeError("授权码失效")),
            FakeAdapter("calendar", [event("c1")]),
        ]
    )
    [result] = run_once(user_id, pg_session, deps=d, now=NOW)

    assert result.failed_sources == ["email"]
    assert result.inserted == 1  # 日历那条进来了
    assert result.pushed is True  # 少几条素材的摘要仍然有用
    assert any("email" in title for title, _ in handles["alerter"].alerts)


def test_normalize_failures_alert_but_do_not_block(pg_session, user_id):
    # 数据源改格式时表现是摘要悄悄变短,没有这条告警只能靠"哪天觉得不对劲"发现
    d, handles = deps(adapters=[FakeAdapter("email", [event("m1"), failed_event("bad")])])
    [result] = run_once(user_id, pg_session, deps=d, now=NOW)

    assert result.normalize_failed == 1
    assert result.pushed is True
    assert any("归一化失败" in title for title, _ in handles["alerter"].alerts)


def test_digest_failure_pushes_nothing_and_marks_the_window_failed(pg_session, user_id):
    """发一条错的比不发更伤信任;记 failed 意味着下次还会重跑。"""
    d, handles = deps(llm=fake_llm({"没有": "summary"}))
    [result] = run_once(user_id, pg_session, deps=d, now=NOW)

    assert result.pushed is False
    assert result.error
    assert handles["channel"].sent == []
    assert (
        pg_session.execute(
            text("SELECT status FROM job_runs WHERE job_name = :j"), {"j": JOB_NAME}
        ).scalar_one()
        == "failed"
    )
    assert handles["alerter"].alerts


def test_push_failure_is_still_logged(pg_session, user_id):
    # "发过但没送到"是排查企微问题时唯一的线索
    d, handles = deps(channel=FakeChannel(boom=RuntimeError("企微超时")))
    [result] = run_once(user_id, pg_session, deps=d, now=NOW)

    assert result.pushed is False
    row = pg_session.execute(text("SELECT delivered, error FROM push_log")).one()
    assert row.delivered is False
    assert "企微超时" in row.error
    assert handles["alerter"].alerts


def test_running_twice_does_not_push_twice(pg_session, user_id):
    """调度器同一天触发两次,第二次什么都不该做。

    注意它走的不是"窗口被占"那条路 —— 上一次已经成功,窗口区间整个在过去,
    windows_to_run 直接算出没有窗口要跑。压根不进 job。
    """
    d, handles = deps()
    run_once(user_id, pg_session, deps=d, now=NOW)
    second = run_once(user_id, pg_session, deps=d, now=NOW)

    assert second == []
    assert len(handles["channel"].sent) == 1


def test_a_crashed_run_is_not_pushed_twice_either(pg_session, user_id):
    """真正会走 skipped 的是这条路:上次认领了窗口但没跑完(进程被 kill)。

    窗口还在,会被重新算出来;但它已经被认领过,所以不会再推一遍。
    这是"窗口是幂等键"那句话唯一真实生效的场景。
    """
    d, handles = deps()
    job_runs.claim_window(
        user_id,
        pg_session,
        job_name=JOB_NAME,
        window_start=NOW - DAY,
        window_end=NOW,
    )
    [result] = run_once(user_id, pg_session, deps=d, now=NOW)

    assert result.skipped is True
    assert handles["channel"].sent == []


def test_llm_fields_sent_is_recorded(pg_session, user_id):
    # R12:"我到底把什么发给外部供应商了"要能回答
    d, _ = deps()
    run_once(user_id, pg_session, deps=d, now=NOW)

    fields = pg_session.execute(text("SELECT llm_fields_sent FROM tool_calls")).scalar_one()
    assert "body" in fields and "标题" in fields
