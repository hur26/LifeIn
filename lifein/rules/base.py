"""规则的接口。

一条规则就是一个纯函数:给它当前时刻和一个只读的会话,它回一批"想说的话"。
**它不推送、不写库、不判断频率** —— 那些是调度层的事(见 `rules/__init__.py`)。

`Reminder.dedup_key` 是这里唯一需要规则自己想清楚的东西:同一件事被同一条
规则扫到多次时,拿什么认出"这是同一件事"。提醒类规则每十几分钟跑一次,
没有它就会把同一场会提醒到开完为止。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from lifein.channels.base import Card


@dataclass(frozen=True)
class RuleContext:
    user_id: str
    session: Session
    now: datetime


@dataclass(frozen=True)
class Reminder:
    """一条"想说的话"。到底说不说得出去,由调度层决定。"""

    card: Card
    dedup_key: str
    """认出"这是同一件事"的键。同一条规则 + 同一个 key 在冷却期内只发一次。"""

    cooldown: timedelta = timedelta(days=1)
    """同一件事多久之内不再提。默认一天 —— 一天提两次同一件事就是骚扰。"""


@dataclass(frozen=True)
class Rule:
    rule_id: str
    """写进 `push_log.rule_id` 与 `rule_state.rule_id`。改名等于换了一条规则,
    影子期的统计会跟着断,所以别改。"""

    description: str
    """一句人话。转 active 之前要给人看的就是它。"""

    evaluate: Callable[[RuleContext], Sequence[Reminder]]
