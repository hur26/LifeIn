"""问答回复的集成测试。

需要真实 PostgreSQL(见 conftest.py)。

重心在**边界**:认不出的人、停用的人、非文字消息、问答失败。正常回答只占
一个用例 —— 它已经在 test_qa_agent.py 里被测透了。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from lifein.channels.base import Card, Delivery, InboundMessage
from lifein.jobs.qa_reply import FAILED_REPLY, UNSUPPORTED_REPLY, QaDeps, handle_message
from lifein.llm.client import LLMClient
from lifein.models.normalized import EventKind, ExternalRef, NormalizedEvent, Trust
from lifein.repos import users
from lifein.repos.raw_events import insert_events
from lifein.sources.base import IngestedEvent

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 7, 9, tzinfo=UTC)
WECOM_ID = "BaiYang"


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


def message(content="报销批了吗", msg_type="text", from_user=WECOM_ID) -> InboundMessage:
    return InboundMessage(
        channel="wecom",
        sender=from_user,
        msg_type=msg_type,
        content=content,
        msg_id="1",
        created_at=NOW,
    )


def fake_llm(payload=None) -> LLMClient:
    out = payload or {"answer": "批了,1280 元。", "refs": ["m1"], "confident": True}
    body = {
        "model": "m",
        "choices": [
            {
                "message": {
                    "content": out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                }
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }
    return LLMClient(
        base_url="https://llm.example.com/v1",
        api_key="k",
        model="m",
        max_retries=0,
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
        ),
        sleep=lambda _s: None,
    )


@pytest.fixture
def registered_user(pg_session) -> str:
    return users.create_user(pg_session, display_name="白杨", wecom_userid=WECOM_ID)


def seed_event(pg_session, user_id: str) -> None:
    insert_events(
        user_id,
        pg_session,
        [
            IngestedEvent(
                source="email",
                external_id="m1",
                occurred_at=NOW - timedelta(hours=3),
                trust=Trust.EXTERNAL,
                raw={},
                normalized=NormalizedEvent(
                    kind=EventKind.MESSAGE,
                    title="报销单已通过",
                    occurred_at=NOW - timedelta(hours=3),
                    external_ref=ExternalRef(source="email", external_id="m1"),
                    trust=Trust.EXTERNAL,
                    confidence=1.0,
                    body="金额 1280 元",
                ),
            )
        ],
    )


def resolve_wecom(session, message):
    return users.find_by_wecom_userid(session, wecom_userid=message.sender)


def deps(channel=None, llm=None) -> tuple[QaDeps, FakeChannel]:
    channel = channel or FakeChannel()
    return (
        QaDeps(llm=llm or fake_llm(), channel=channel, resolve_user=resolve_wecom),
        channel,
    )


def test_question_gets_answered(pg_session, registered_user):
    seed_event(pg_session, registered_user)
    d, channel = deps()

    result = handle_message(pg_session, message=message(), deps=d, now=NOW)

    assert result.handled is True
    assert result.user_id == registered_user
    assert channel.sent[0].summary == "批了,1280 元。"
    assert channel.sent[0].title == "报销批了吗"  # 问题放标题,对话流里对得上


def test_unknown_sender_gets_no_reply_at_all(pg_session):
    """回一句"你不是这个系统的用户"等于告诉对方这个地址是活的。"""
    d, channel = deps()
    result = handle_message(pg_session, message=message(from_user="陌生人"), deps=d, now=NOW)

    assert result.handled is False
    assert result.reason == "unknown_user"
    assert channel.sent == []


def test_disabled_user_is_ignored(pg_session, registered_user):
    pg_session.execute(
        text("UPDATE users SET disabled_at = now() WHERE id = :id"), {"id": registered_user}
    )
    d, channel = deps()
    result = handle_message(pg_session, message=message(), deps=d, now=NOW)

    assert result.reason == "disabled"
    assert channel.sent == []


def test_non_text_message_gets_a_plain_explanation(pg_session, registered_user):
    d, channel = deps()
    result = handle_message(pg_session, message=message(msg_type="image"), deps=d, now=NOW)

    assert result.reason == "unsupported_type"
    assert channel.sent[0].summary == UNSUPPORTED_REPLY


def test_qa_failure_replies_in_plain_language(pg_session, registered_user):
    # 用户不需要知道是解析失败还是超时
    seed_event(pg_session, registered_user)
    d, channel = deps(llm=fake_llm("我觉得应该批了吧"))
    result = handle_message(pg_session, message=message(), deps=d, now=NOW)

    assert result.reason == "qa_failed"
    assert channel.sent[0].summary == FAILED_REPLY


def test_reply_is_not_written_to_push_log(pg_session, registered_user):
    """回复不占频率额度 —— 否则多问几句就把当天的摘要额度吃光了。"""
    seed_event(pg_session, registered_user)
    d, _ = deps()
    handle_message(pg_session, message=message(), deps=d, now=NOW)

    assert pg_session.execute(text("SELECT count(*) FROM push_log")).scalar_one() == 0


def test_llm_call_is_audited(pg_session, registered_user):
    seed_event(pg_session, registered_user)
    d, _ = deps()
    handle_message(pg_session, message=message(), deps=d, now=NOW)

    row = pg_session.execute(text("SELECT agent, args_digest FROM tool_calls")).one()
    assert row.agent == "qa"
    # 记的是问题长度不是问题本身
    assert row.args_digest["question_len"] == len("报销批了吗")
    assert "报销批了吗" not in str(row.args_digest)


def test_send_failure_does_not_raise(pg_session, registered_user):
    # 他可以再问一次,不像每日摘要那样"错过就没了"
    seed_event(pg_session, registered_user)
    d, _ = deps(channel=FakeChannel(boom=RuntimeError("企微超时")))
    result = handle_message(pg_session, message=message(), deps=d, now=NOW)
    assert result.handled is True


def test_empty_question_is_dropped(pg_session, registered_user):
    d, channel = deps()
    result = handle_message(pg_session, message=message(content="   "), deps=d, now=NOW)
    assert result.reason == "empty"
    assert channel.sent == []


def test_only_events_inside_the_lookback_window_are_used(pg_session, registered_user):
    seed_event(pg_session, registered_user)
    d, _ = deps()
    handle_message(pg_session, message=message(), deps=d, now=NOW, lookback=timedelta(minutes=1))
    # 事件在 3 小时前,窗口只有 1 分钟 —— 模型拿到的是空素材,
    # 于是 refs 里那个 m1 无效,答案被标成没依据
    row = pg_session.execute(text("SELECT args_digest FROM tool_calls")).scalar_one()
    assert row["events"]["len"] == 0
