"""推送通道接口 —— 架构 §9.4 的扩展点。

企微与邮件实现同一个接口。**安卓 App 不是推送通道** —— 它不接收推送
(ADR-014)。接口留出来是为了平台政策变动时能换成别的(R6),不是为了
现在就做多通道。

`Card` 是通道中立的:它描述"要说什么",不描述"长什么样"。渲染是各通道
自己的事 —— 企微渲染成 markdown,邮件渲染成 HTML。**卡片里不许出现任何
企微特有的字段**,否则换通道时要改的就不止一个文件了。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class CardSection:
    """一段内容。`heading` 可以为空,那就是一段没有小标题的正文。"""

    lines: Sequence[str]
    heading: str | None = None


@dataclass(frozen=True)
class Card:
    """一条推送的内容。

    `summary` 是那种"只看这一句也不亏"的话。手机通知栏里能看到的往往只有
    标题和它 —— 摘要写得再全,用户先看到的也是这一行。
    """

    title: str
    summary: str
    sections: Sequence[CardSection] = field(default_factory=tuple)
    footer: str | None = None


@dataclass(frozen=True)
class Delivery:
    channel: str
    delivery_id: str
    truncated: bool = False
    """内容被通道的长度上限截断过。要记进 push_log —— 摘要变短可能是它导致的,
    不是模型偷懒。"""


class Channel(Protocol):
    name: str

    def send(self, user_id: str, card: Card) -> Delivery:
        """把卡片发给这个用户。

        通道自己负责把 `user_id` 解析成本通道的地址(企微 userid、邮箱地址)。
        调用方只认识 `user_id`,不该知道对面的寻址方式。
        """
        ...
