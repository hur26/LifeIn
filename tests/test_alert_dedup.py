"""告警收敛(ADR-031)。

2026-09-10 同一封告警发了 **85 封**,一字不差。第 85 封没有比第 1 封多说
任何东西 —— 它唯一的作用是让前面 84 封更难被看见,而 07 §2.7 把邮件称作
**告警的唯一出口**:一个被同一件事刷屏的出口,和堵住了没有区别。

这一组盯住四件事,每一件都能单独把这个机制变成坏东西:

- 收敛了不该收敛的(标题一样但内容不同 → 另一个人的故障消失了)
- 收敛之后不说压了多少(那就是在制造安静,而安静是这个项目最危险的失效)
- 连日志也一起收敛(排查时那个"85"就是从日志里数出来的)
- 关不掉(收敛本身出错时要能一键退回"每一条都发")

不需要数据库:时钟是假的,出口也是假的。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from lifein.alerts import CollectingAlerter, ThrottledAlerter

START = datetime(2026, 9, 10, 13, 0, tzinfo=UTC)


class Clock:
    """能被推着走的时钟。**真等六十分钟测不出任何东西。**"""

    def __init__(self, at: datetime = START) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def tick(self, minutes: int) -> None:
        self.at += timedelta(minutes=minutes)


@pytest.fixture
def wired():
    inner = CollectingAlerter()
    clock = Clock()
    return ThrottledAlerter(inner, window_m=60, now=clock), inner, clock


class TestWhatGetsSent:
    def test_第一封照发(self, wired):
        throttled, inner, _ = wired
        throttled.alert("审批执行异常", "column started_at does not exist")

        assert inner.alerts == [("审批执行异常", "column started_at does not exist")]

    def test_窗口内一模一样的那些不再发(self, wired):
        throttled, inner, clock = wired
        for _ in range(20):
            throttled.alert("审批执行异常", "column started_at does not exist")
            clock.tick(3)  # 审批 job 就是三分钟一轮

        assert len(inner.alerts) == 1

    def test_差一个字就是另一条告警(self, wired):
        """**这是这个机制最容易做错的地方。**

        P4 之后 `user=A: 授权码失效` 和 `user=B: 授权码失效` 是两个人的
        两件事。只按标题去重的话,第二个人的故障从来不会被报告。
        """
        throttled, inner, _ = wired
        throttled.alert("采集器掉线", "user=A: 两小时没有心跳")
        throttled.alert("采集器掉线", "user=B: 两小时没有心跳")

        assert len(inner.alerts) == 2

    def test_标题不同也各发各的(self, wired):
        throttled, inner, _ = wired
        throttled.alert("审批执行异常", "同一段正文")
        throttled.alert("月度报告异常", "同一段正文")

        assert len(inner.alerts) == 2

    def test_窗口过了就再发一封(self, wired):
        throttled, inner, clock = wired
        throttled.alert("审批执行异常", "还是那句话")
        clock.tick(61)
        throttled.alert("审批执行异常", "还是那句话")

        assert len(inner.alerts) == 2

    def test_窗口配零等于不去重(self):
        """收敛本身也可能出错,那时要能一键退回"每一条都发"。"""
        inner = CollectingAlerter()
        throttled = ThrottledAlerter(inner, window_m=0, now=Clock())
        for _ in range(5):
            throttled.alert("审批执行异常", "还是那句话")

        assert len(inner.alerts) == 5


class TestItSaysHowManyItHeld:
    """**一个不说自己压了多少的收敛器,就是在制造安静。**"""

    def test_第二封带上这段时间里压住了几次(self, wired):
        throttled, inner, clock = wired
        throttled.alert("审批执行异常", "还是那句话")
        for _ in range(19):
            clock.tick(3)
            throttled.alert("审批执行异常", "还是那句话")
        clock.tick(10)  # 到这里已经过了 67 分钟
        throttled.alert("审批执行异常", "还是那句话")

        assert len(inner.alerts) == 2
        assert "还发生了 19 次" in inner.alerts[1][1]

    def test_没压住过的那封不带这句话(self, wired):
        throttled, inner, clock = wired
        throttled.alert("审批执行异常", "还是那句话")
        clock.tick(61)
        throttled.alert("审批执行异常", "还是那句话")

        assert "还发生了" not in inner.alerts[1][1]

    def test_原文一个字都不许丢(self, wired):
        throttled, inner, clock = wired
        throttled.alert("审批执行异常", "column started_at does not exist")
        clock.tick(3)
        throttled.alert("审批执行异常", "column started_at does not exist")
        clock.tick(61)
        throttled.alert("审批执行异常", "column started_at does not exist")

        assert inner.alerts[1][1].startswith("column started_at does not exist")


class TestLogsAreNotThrottled:
    """**收敛的只有出口。** 日志在本机、不打扰任何人,而且它是排查时唯一能
    数出"到底发生了多少次"的地方 —— 这次那个 85 就是从日志里数出来的。"""

    def test_被压住的那些照样写一行(self, wired, caplog):
        throttled, _, clock = wired
        with caplog.at_level(logging.ERROR, logger="lifein.alert"):
            for _ in range(5):
                throttled.alert("审批执行异常", "还是那句话")
                clock.tick(3)

        # 一次一行:发出去的那封由出口自己记(CollectingAlerter 不记),
        # 被压住的四次由收敛器记
        held = [r for r in caplog.records if "这一封不发了" in r.getMessage()]
        assert len(held) == 4

    def test_日志里说得出这是第几次(self, wired, caplog):
        throttled, _, clock = wired
        with caplog.at_level(logging.ERROR, logger="lifein.alert"):
            throttled.alert("审批执行异常", "还是那句话")
            clock.tick(3)
            throttled.alert("审批执行异常", "还是那句话")

        assert any("第 2 次" in r.getMessage() for r in caplog.records)


class TestTheIncident:
    def test_那一天的八十五封会变成五封(self, wired):
        """**事故复现。** 审批 job 三分钟一轮,连着炸了四个多小时。

        85 封变成 5 封,而那 5 封里的后 4 封各自带着"这段时间还发生了 N 次"
        —— 收敛掉的是重复,不是信息。
        """
        throttled, inner, clock = wired
        for _ in range(85):
            throttled.alert(
                "审批执行异常",
                "user=3b61c626: ProgrammingError: column started_at does not exist",
            )
            clock.tick(3)

        assert len(inner.alerts) == 5
        assert all("还发生了" in detail for _title, detail in inner.alerts[1:])

    def test_键不会无限攒着(self, wired):
        """每种新告警都留一个键,不清理的话这个 dict 会一直长。"""
        throttled, _, clock = wired
        for i in range(50):
            throttled.alert("覆盖率巡检异常", f"第 {i} 个不同的来源")
            clock.tick(10)

        # 两个窗口之前的都该忘掉了。留两个窗口而不是一个,是为了那句
        # "还发生了 N 次" 还带得出来
        assert len(throttled._sent) < 20
