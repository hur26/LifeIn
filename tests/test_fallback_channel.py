"""多通道降级的测试。

最要紧的一条是 `test_falling_back_alerts`:降级如果不告警,摘要照常送到,
你不会察觉任何异常,而主通道可能已经死了两周 —— 等兜底也坏的那天才发现
两条路都是坏的。
"""

from __future__ import annotations

import pytest

from lifein.alerts import CollectingAlerter
from lifein.channels.base import Card, Delivery
from lifein.channels.fallback import AllChannelsFailed, FallbackChannel

CARD = Card(title="摘要", summary="今天有 2 件事")
USER = "11111111-1111-1111-1111-111111111111"


class FakeChannel:
    def __init__(self, name: str, boom: Exception | None = None) -> None:
        self.name = name
        self._boom = boom
        self.calls = 0

    def send(self, user_id, card) -> Delivery:
        self.calls += 1
        if self._boom:
            raise self._boom
        return Delivery(channel=self.name, delivery_id=f"{self.name}-1")


def build(*channels) -> tuple[FallbackChannel, CollectingAlerter]:
    alerter = CollectingAlerter()
    return FallbackChannel(list(channels), alerter=alerter), alerter


def test_primary_wins_and_backup_is_untouched():
    primary, backup = FakeChannel("weixin"), FakeChannel("wecom")
    channel, alerter = build(primary, backup)

    assert channel.send(USER, CARD).channel == "weixin"
    assert backup.calls == 0
    assert alerter.alerts == []  # 一切正常时不该有任何噪音


def test_falls_back_when_primary_fails():
    primary = FakeChannel("weixin", boom=RuntimeError("会话过期"))
    backup = FakeChannel("wecom")
    channel, _ = build(primary, backup)

    assert channel.send(USER, CARD).channel == "wecom"
    assert backup.calls == 1


def test_falling_back_alerts():
    """摘要照常送到了,你不会察觉异常 —— 所以必须主动告诉你。"""
    primary = FakeChannel("weixin", boom=RuntimeError("会话过期"))
    channel, alerter = build(primary, FakeChannel("wecom"))
    channel.send(USER, CARD)

    assert len(alerter.alerts) == 1
    title, detail = alerter.alerts[0]
    assert "降级" in title and "wecom" in title
    assert "会话过期" in detail  # 要说清主通道是怎么坏的


def test_all_failing_raises_and_alerts():
    # 一条都没送出去 = 当天没有摘要,必须让调用方知道并记进 push_log
    channel, alerter = build(
        FakeChannel("weixin", boom=RuntimeError("过期")),
        FakeChannel("wecom", boom=RuntimeError("超时")),
    )
    with pytest.raises(AllChannelsFailed) as exc:
        channel.send(USER, CARD)

    assert "weixin" in str(exc.value) and "wecom" in str(exc.value)
    assert alerter.alerts[0][0] == "所有推送通道都失败"


def test_name_is_the_primary_but_delivery_records_the_real_one():
    """push_log 记的是真正送达的那个通道,不是这个包装类的名字。"""
    channel, _ = build(FakeChannel("weixin", boom=RuntimeError("x")), FakeChannel("wecom"))
    assert channel.name == "weixin"
    assert channel.send(USER, CARD).channel == "wecom"


def test_single_channel_still_works():
    channel, _ = build(FakeChannel("wecom"))
    assert channel.send(USER, CARD).channel == "wecom"


def test_empty_channel_list_is_rejected():
    with pytest.raises(ValueError):
        FallbackChannel([], alerter=CollectingAlerter())


def test_third_channel_is_reached():
    channel, _ = build(
        FakeChannel("a", boom=RuntimeError("1")),
        FakeChannel("b", boom=RuntimeError("2")),
        FakeChannel("c"),
    )
    assert channel.send(USER, CARD).channel == "c"
