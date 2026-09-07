"""数据源适配器接口 —— 架构 §9.1 的扩展点。

**加一个数据源,只应该改这一处**:实现一个适配器 + 写一段归一化。
不改事件流、不改 agent、不改推送。归一化写不出来,说明这个源还没想清楚,
不要先接进来再说。

两种形态:拉取型(邮箱、日历,定时轮询)与推送型(采集器 webhook,P1 起)。
它们的差别只在"事件从哪来",产出完全一样 —— 都是 `IngestedEvent`。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from lifein.models.normalized import NormalizedEvent, Trust


class NormalizeFailed(Exception):
    """归一化失败。**绝不静默丢弃**(06 §1.4)。

    抛它意味着"必填字段缺失"这一档:事件仍然入库,`raw` 保留,
    但 `normalized` 留空、`normalize_error` 写原因,并且要告警。
    解析器修好之后可以按 (user_id, source, external_id) 幂等重跑。
    """


@dataclass(frozen=True)
class IngestedEvent:
    """适配器的产出。对应 `raw_events` 一行。

    `raw` 永远保留 —— 它是重跑的依据,也是"我到底收到了什么"的唯一答案。
    """

    source: str
    external_id: str
    occurred_at: datetime
    trust: Trust
    raw: dict[str, Any]
    normalized: NormalizedEvent | None = None
    normalize_error: str | None = None
    attempted_at: datetime | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if (self.normalized is None) == (self.normalize_error is None):
            # 两个都有等于说不清这条到底能不能用;两个都没有等于悄悄丢了
            raise ValueError("normalized 与 normalize_error 必须恰好有一个")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at 必须带时区")

    @property
    def failed(self) -> bool:
        return self.normalize_error is not None


@runtime_checkable
class PullAdapter(Protocol):
    """拉取型:定时问数据源要新东西。邮箱、日历属于这一类。"""

    source: str

    def fetch(self, since: datetime) -> Iterable[IngestedEvent]:
        """取 `since` 之后的事件。

        实现必须容忍重复返回 —— 去重靠 `(user_id, source, external_id)` 唯一键,
        不靠适配器记住上次取到哪。适配器无状态,重启后行为一致。
        """
        ...


@runtime_checkable
class PushAdapter(Protocol):
    """推送型:数据源主动送上门。P1 的安卓采集器属于这一类。"""

    source: str

    def handle(self, payload: Mapping[str, Any]) -> Iterable[IngestedEvent]:
        """把一次上报解成事件。

        一次上报可能包含多条(采集器攒了一批离线补报),所以返回的是列表。
        """
        ...
