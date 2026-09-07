"""企业微信推送通道。

P0 用 markdown 消息,不用模板卡片:卡片必须带 `card_action` 跳转目标,
而 P0 没有可跳的地方(App 在 P1,Web 控制台在 P4)。卡片真正不可替代的
是按钮回调,那是审批的硬需求,和审批一起在 P3 做(03 P0 范围)。

**企微 markdown 的两个坑,都在这里处理掉:**

一是长度。markdown 内容上限 2048 **字节**,不是字符 —— 中文一个字三字节,
所以七百来字就到顶了。超了服务端直接报错,整条推送丢失。宁可截断也不能丢:
每天一条摘要,丢一条就是那一天什么都没有。

二是语法子集。企微的 markdown 不支持列表、表格、图片,支持的只有标题、
加粗、引用、链接、行内代码和字体颜色。所以列表用 `·` 手工渲染成普通行,
不写 `-` —— 写了会原样显示成一个减号。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from lifein.channels.base import Card, Delivery
from lifein.channels.wecom_client import WecomClient

log = logging.getLogger(__name__)

MARKDOWN_MAX_BYTES = 2048
"""企微 markdown 消息体上限,**按 UTF-8 字节算**。"""

TRUNCATION_NOTE = "\n\n> 内容过长已截断"

_BULLET = "· "


class WecomChannel:
    name = "wecom"

    def __init__(
        self,
        client: WecomClient,
        *,
        resolve_userid: Callable[[str], str],
    ) -> None:
        self._client = client
        # user_id → 企微 userid。调用方只认识 user_id,不该知道对面的寻址方式;
        # 映射存在 users.wecom_userid(06 §2.10)。
        self._resolve_userid = resolve_userid

    def send(self, user_id: str, card: Card) -> Delivery:
        content, truncated = render_markdown(card)
        payload = {
            "touser": self._resolve_userid(user_id),
            "msgtype": "markdown",
            "agentid": self._client.agent_id,
            "markdown": {"content": content},
            # 不做重复消息检查:每日摘要天天结构相似,让企微去判重会误杀
            "enable_duplicate_check": 0,
        }
        data = self._client.post("/message/send", payload)
        if truncated:
            log.warning("推送内容超过 %d 字节,已截断", MARKDOWN_MAX_BYTES)
        return Delivery(
            channel=self.name,
            delivery_id=str(data.get("msgid", "")),
            truncated=truncated,
        )


def render_markdown(card: Card) -> tuple[str, bool]:
    """把通道中立的 Card 渲染成企微 markdown。返回(内容, 是否截断)。"""
    blocks: list[str] = [f"# {card.title}"]
    if card.summary:
        blocks.append(card.summary)

    for section in card.sections:
        lines: list[str] = []
        if section.heading:
            lines.append(f"**{section.heading}**")
        # 企微 markdown 不支持列表,用 · 渲染成普通行
        lines.extend(f"{_BULLET}{line}" for line in section.lines)
        if lines:
            blocks.append("\n".join(lines))

    if card.footer:
        blocks.append(f"> {card.footer}")

    return _fit(("\n\n".join(blocks)).strip())


def _fit(content: str) -> tuple[str, bool]:
    """按字节截断。

    在字符边界上切,不在字节边界上切 —— 后者会切出半个汉字,
    而那半个字节会让整条消息的编码失效。
    """
    if len(content.encode()) <= MARKDOWN_MAX_BYTES:
        return content, False

    budget = MARKDOWN_MAX_BYTES - len(TRUNCATION_NOTE.encode())
    kept: list[str] = []
    used = 0
    for char in content:
        size = len(char.encode())
        if used + size > budget:
            break
        kept.append(char)
        used += size
    return "".join(kept).rstrip() + TRUNCATION_NOTE, True
