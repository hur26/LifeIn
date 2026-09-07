"""告警出口。

**这个项目最危险的失效方式不是崩溃,是安静。** 采集器掉线、数据源改格式、
授权码失效 —— 这几件事的共同表现都是"提醒悄悄变少",系统看上去一切正常
(R8 / R9)。所以凡是"少做了一件事"的地方都要走这里。

07 §2.6 定的 `ALERT_CHANNEL` 默认是 email,理由是告警不能走可能已经挂掉的
企微。但**邮件兜底通道要到 P1 才有**(01 §4),所以 P0 只有日志实现。
这是排期,不是遗漏 —— P0 单机自己用,`journalctl` 看得见。
"""

from __future__ import annotations

import logging
from typing import Protocol

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


class CollectingAlerter:
    """测试用。断言"该告警的时候确实告警了"比断言日志内容可靠。"""

    def __init__(self) -> None:
        self.alerts: list[tuple[str, str]] = []

    def alert(self, title: str, detail: str) -> None:
        self.alerts.append((title, detail))
