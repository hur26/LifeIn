"""按顺序尝试多个推送通道,前面的失败就用后面的。

[ADR-018](../../docs/04-tech-decisions.md) 把微信定为主通道、企微定为兜底。
这个类就是那句话的实现,它自己不认识任何具体通道 —— 换成飞书或别的
也是同一个类([R6](../../docs/05-risks.md) 说的平台政策变动)。

**降级必须告警。** 这是整个类里最要紧的一条:摘要照常送到了,你不会察觉
任何异常,而主通道可能已经死了两周。等到兜底通道也出问题的那天,你才发现
自己一直靠备胎在跑 —— 那时候两条路都是坏的。

**全部失败时抛最后一个异常。** 不吞:一条都没送出去,那是当天没有摘要,
必须让调用方知道并记进 `push_log`。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from lifein.alerts import Alerter
from lifein.channels.base import Card, Channel, Delivery

log = logging.getLogger(__name__)


class AllChannelsFailed(RuntimeError):
    """所有通道都发不出去。当天没有摘要。"""


class FallbackChannel:
    def __init__(self, channels: Sequence[Channel], *, alerter: Alerter) -> None:
        if not channels:
            raise ValueError("至少要有一个通道")
        self._channels = list(channels)
        self._alerter = alerter

    @property
    def name(self) -> str:
        # 名字是主通道的名字。真正送达的是哪个看 Delivery.channel ——
        # push_log 记的也是那个,不是这里
        return self._channels[0].name

    def send(self, user_id: str, card: Card) -> Delivery:
        failures: list[str] = []
        last_error: Exception | None = None

        for index, channel in enumerate(self._channels):
            try:
                delivery = channel.send(user_id, card)
            except Exception as exc:  # noqa: BLE001
                log.warning("通道 %s 发送失败:%s", channel.name, exc)
                failures.append(f"{channel.name}: {type(exc).__name__}: {exc}")
                last_error = exc
                continue

            if index > 0:
                # 送到了,但不是从主通道走的。不告警的话你永远不知道主通道死了
                self._alerter.alert(
                    f"推送降级到 {channel.name}",
                    "前面的通道失败了:" + "；".join(failures),
                )
            return delivery

        self._alerter.alert("所有推送通道都失败", "；".join(failures))
        raise AllChannelsFailed("；".join(failures)) from last_error
