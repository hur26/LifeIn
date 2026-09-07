"""摄入事件结构的测试。

这个结构只有两条规则,但两条都对应"数据悄悄消失"这种最难查的故障。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lifein.models.normalized import EventKind, ExternalRef, NormalizedEvent, Trust
from lifein.sources.base import IngestedEvent

NOW = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)


def ok_event() -> NormalizedEvent:
    return NormalizedEvent(
        kind=EventKind.MESSAGE,
        title="报销单已通过",
        occurred_at=NOW,
        external_ref=ExternalRef(source="email", external_id="m1"),
        trust=Trust.EXTERNAL,
        confidence=0.95,
    )


def build(**overrides) -> IngestedEvent:
    base = dict(
        source="email",
        external_id="m1",
        occurred_at=NOW,
        trust=Trust.EXTERNAL,
        raw={"subject": "报销单已通过"},
        normalized=ok_event(),
    )
    return IngestedEvent(**{**base, **overrides})


def test_normalized_event_is_not_failed():
    assert build().failed is False


def test_failed_event_keeps_raw():
    # 归一化失败绝不静默丢弃:raw 保留,修好解析器后能重跑
    e = build(normalized=None, normalize_error="缺 Date 头")
    assert e.failed is True
    assert e.raw == {"subject": "报销单已通过"}


def test_both_normalized_and_error_is_rejected():
    # 两个都有,等于说不清这条到底能不能用
    with pytest.raises(ValueError):
        build(normalize_error="缺 Date 头")


def test_neither_normalized_nor_error_is_rejected():
    # 两个都没有,等于悄悄丢了
    with pytest.raises(ValueError):
        build(normalized=None)


def test_naive_occurred_at_is_rejected():
    with pytest.raises(ValueError):
        build(occurred_at=datetime(2026, 9, 7, 8, 0))
