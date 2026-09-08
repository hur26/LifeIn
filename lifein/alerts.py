"""告警出口。

**这个项目最危险的失效方式不是崩溃,是安静。** 采集器掉线、数据源改格式、
授权码失效 —— 这几件事的共同表现都是"提醒悄悄变少",系统看上去一切正常
(R8 / R9)。所以凡是"少做了一件事"的地方都要走这里。

07 §2.6 定的 `ALERT_CHANNEL` 默认是 email,理由是告警不能走可能已经挂掉的
那条推送通道 —— 而"挂掉"恰恰是最需要告警的时候。P1 起邮件通道有了,
`EmailAlerter` 就是那句话的落地;没配 SMTP 时仍然退回日志实现。

**告警自己失败了不许往上抛。** 告警是"某件事没做好"的附属动作,
让它把调用方一起拖垮,等于用一个小问题换一个大问题。所以这里捕获异常
并降级到日志 —— 日志至少还在本机。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from lifein.channels.base import Card, Channel

log = logging.getLogger("lifein.alert")


class Alerter(Protocol):
    def alert(self, title: str, detail: str) -> None: ...


class LoggingAlerter:
    """P0 的实现:写 ERROR 日志。

    刻意用固定的 logger 名 `lifein.alert`,这样部署时可以只对它配一条转发规则,
    不用去筛全部日志。
    """

    def alert(self, title: str, detail: str) -> None:
        log.error("[告警] %s —— %s", title, detail)


class EmailAlerter:
    """P1 的实现:发一封邮件,同时照样写日志。

    **两条都留。** 邮件可能延迟、可能进垃圾箱,而日志是本机的、立刻可查;
    反过来日志需要有人去看,邮件会主动找到你。两者的失效方式不重叠,
    这正是"兜底"的意思。
    """

    def __init__(self, channel: Channel, user_id_provider: Callable[[], str | None]) -> None:
        self._channel = channel
        self._user_id_provider = user_id_provider
        """告警发给谁。用回调而不是固定值:进程启动时可能还没有用户,
        而 P4 多用户之后"发给谁"也不再是一个常量。"""

    def alert(self, title: str, detail: str) -> None:
        log.error("[告警] %s —— %s", title, detail)
        try:
            user_id = self._user_id_provider()
            if user_id is None:
                return
            self._channel.send(
                user_id,
                Card(title=f"告警:{title}", summary=detail, footer="来自 LifeIn 自检"),
            )
        except Exception:  # noqa: BLE001
            # 告警自己失败不许拖垮调用方:那是用一个小问题换一个大问题
            log.exception("告警邮件发送失败,只剩日志了")


class CollectingAlerter:
    """测试用。断言"该告警的时候确实告警了"比断言日志内容可靠。"""

    def __init__(self) -> None:
        self.alerts: list[tuple[str, str]] = []

    def alert(self, title: str, detail: str) -> None:
        self.alerts.append((title, detail))
