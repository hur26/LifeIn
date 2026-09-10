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

**同一条告警不重复发**(ADR-031)。2026-09-10 那次事故里同一封信发了 85 封,
一字不差 —— 第 85 封没有比第 1 封多说任何东西,只是让前面 84 封更难被看见。
`ThrottledAlerter` 包在出口外面做这件事,而**它只收敛出口,不收敛日志**:
日志在本机、不打扰任何人,而且排查时那个"85"就是从日志里数出来的。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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


@dataclass
class _Sent:
    """某一条告警上一次真的发出去是什么时候,以及那之后压住了几条。"""

    at: datetime
    suppressed: int = 0


class ThrottledAlerter:
    """同一条告警在一个窗口内只发一封(ADR-031)。

    **键是 `(title, detail)` 完全一致,差一个字就是另一条告警。** 这是刻意保守:
    P4 之后 `user=A: 授权码失效` 和 `user=B: 授权码失效` 是两个人的两件事,
    只按标题去重会让其中一个人的故障消失。**宁可少收敛,不要错合并** ——
    少收敛的代价是多几封信,错合并的代价是一件事从来没有被报告过。

    **被压住的那些照样写一行 ERROR 日志。** 收敛的只有出口:日志在本机、
    不打扰任何人,而且它是排查时唯一能数出"到底发生了多少次"的地方。

    **窗口结束后的第一封会带上被压住的次数。** 一个不说自己压了多少的收敛器,
    就是在制造安静 —— 而这个项目最危险的失效方式正是安静(见本模块开头)。
    """

    def __init__(
        self,
        inner: Alerter,
        *,
        window_m: int,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._inner = inner
        self._window = timedelta(minutes=max(window_m, 0))
        self._now = now or (lambda: datetime.now(UTC))
        self._sent: dict[tuple[str, str], _Sent] = {}
        self._lock = threading.Lock()
        """调度器用线程池跑 job,入站还各有一个线程 —— 这个 dict 会被并发碰到。"""

    def alert(self, title: str, detail: str) -> None:
        if not self._window:
            # 配成 0 就是不去重。留这条路是因为收敛本身也可能出错,
            # 而那时要能一键退回"每一条都发"
            self._inner.alert(title, detail)
            return

        now = self._now()
        key = (title, detail)
        with self._lock:
            self._forget_old(now)
            seen = self._sent.get(key)
            if seen is not None and now - seen.at < self._window:
                seen.suppressed += 1
                log.error(
                    "[告警] %s —— %s(窗口内第 %d 次,这一封不发了)",
                    title,
                    detail,
                    seen.suppressed + 1,
                )
                return
            held = seen.suppressed if seen is not None else 0
            self._sent[key] = _Sent(at=now)

        # **发信在锁外面。** SMTP 可能要几秒,而拿着锁发信会让别的告警排队 ——
        # 代价是两条同时进来的相同告警可能都发出去,那比"告警互相阻塞"轻
        self._inner.alert(title, self._with_count(detail, held))

    def _with_count(self, detail: str, held: int) -> str:
        if held == 0:
            return detail
        minutes = int(self._window.total_seconds() // 60)
        return (
            detail
            + "\n\n"
            + f"(过去 {minutes} 分钟里同样的告警还发生了 {held} 次,"
            + "那几次只写了日志没有发信)"
        )

    def _forget_old(self, now: datetime) -> None:
        """把过了两个窗口还没再来的键丢掉。

        不丢的话这个 dict 会随着"出现过多少种不同的告警"一直长。两个窗口
        而不是一个:刚过窗口就忘掉的话,那条"还发生了 N 次"就带不出来了。
        """
        cutoff = now - self._window * 2
        stale = [key for key, sent in self._sent.items() if sent.at < cutoff]
        for key in stale:
            del self._sent[key]


class CollectingAlerter:
    """测试用。断言"该告警的时候确实告警了"比断言日志内容可靠。"""

    def __init__(self) -> None:
        self.alerts: list[tuple[str, str]] = []

    def alert(self, title: str, detail: str) -> None:
        self.alerts.append((title, detail))
