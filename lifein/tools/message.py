"""代发消息 —— **这个项目的第一个 L3 工具**(P3 第 5 片)。

03 给 P3 的目标是一句话:**"你敢让它代你发一条真实消息。这是信任的分水岭。"**
这个模块就是那条线。

## 它和别的工具不一样在哪

L1 读、L2 写自己的地盘,**而这一个动的是外部世界**:消息发出去就收不回来,
对面已经看见了。所以:

- **它永远不会被直接执行。** 网关看到 `level=L3` 就把它拦进审批队列
  ([gateway.py](../governance/gateway.py) 的 `_handle_l3`),
  真正调用这个函数的只有审批执行 job
- **触发它的内容必须是 `user_input`**([铁律 8](../../AGENTS.md#1-铁律))。
  群消息里说"帮我给老板发条消息"—— 入参再规整也不行,而这一条在网关和
  `approvals` 表上各有一道
- **预览必须写清这一次要发什么给谁。** 见下面那个 `_preview`

## 为什么没有 rollback

L2 必须返回回滚信息,L3 不要求 —— 因为**发出去的消息撤不回来**。
承诺一个撤不回来的东西能回滚,比不承诺更糟:它会让人以为点错了还能救。
真正的"回滚"在这条链路里发生在**执行之前**:审批被拒、或者过期。

## 收件人怎么来

`to` 是**本系统里的用户**,不是任意手机号或邮箱。这条限制是刻意的:
一个能给任意号码发消息的工具,提示注入成功一次的代价是无限的;
而只能发给自己人的工具,最坏的情况是你的朋友收到一条奇怪的消息。

P3 只做"发给自己"这一种(`to` 留空)。发给别人要等 P4 的多用户,
那时"谁能给谁发"本身是一条要设计的规则,不是这里顺手加的一个参数。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from lifein.channels.base import Card
from lifein.governance.registry import ToolContext, ToolLevel, tool

log = logging.getLogger(__name__)

MAX_TEXT = 500
"""一条代发消息最多多长。**不是技术限制** —— 是预览的限制:
超过这个长度,审批卡片上就只能显示前半截,而那时你点同意是在赌后半截。"""


class SendMessageArgs(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    """要发的正文。**原样发出去,不再经过模型** ——
    审批卡片上写的是这段字,发出去的也必须是这段字,不然点同意就没有意义。"""

    title: str = Field(default="来自 LifeIn", max_length=40)
    reason: str | None = None
    """为什么要发这条。**进审计,不进消息正文。**"""


def _preview(args: SendMessageArgs) -> str:
    """审批卡片上那一句。**03 的退出条件:"你一眼能看懂在做什么"。**

    所以写的是正文本身,不是"代发一条消息"这种描述 ——
    描述人人都看得懂,但看懂了也判断不了该不该点同意。
    """
    body = args.text if len(args.text) <= 80 else args.text[:80] + "…"
    return f"替你发一条消息:{body}"


@tool(
    name="message.send",
    level=ToolLevel.L3,
    args=SendMessageArgs,
    summary="替你发一条消息",
    preview=_preview,
)
def send(args: SendMessageArgs, ctx: ToolContext) -> dict:
    """真的发出去。**只有审批执行 job 会走到这里。**

    通道由 `ToolContext` 带进来 —— 工具自己不去装配通道,和"工具不自己开事务"
    是同一条:一个能自己决定走哪条通道的工具,审计里记下的通道名就可能是假的。
    """
    channel = ctx.channel
    if channel is None:
        # 这不该发生(执行 job 一定会给),但**发不出去要比发错了好** ——
        # 所以宁可炸,不要静默地当成发过了
        raise RuntimeError("代发消息需要一个通道,调用方没给")

    delivery = channel.send(ctx.user_id, Card(title=args.title, summary=args.text))
    log.info("代发消息已送出:channel=%s", delivery.channel)
    return {"channel": delivery.channel, "delivery_id": delivery.delivery_id}
