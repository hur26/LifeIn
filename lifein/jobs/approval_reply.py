"""审批的回复入口(P3 第 6 片)。**"同意 12" 走到这里,不进问答。**

03 写的是"企微交互卡片回调链路(签名校验、时效校验)"。签名和时效那两道
**已经在** `channels/wecom_callback.py` 里,而且是 P0 就建好并且每天在跑的 ——
这一片要做的是它后面那一段:**把"同意 12"变成一次状态变更**。

## 为什么是回文字,不是点按钮

03 那句话里的"交互卡片"是企微的一个具体功能,而**主推送通道是微信 iLink**
([ADR-018](../../docs/04-tech-decisions.md#adr-018--微信推送走-ilink-bot-api企微降为兜底与审批入口)),
那条通道上根本没有按钮。做成只能点按钮的话:

- 平时收到的是微信里一段文字,上面没有按钮
- 要批准得先切到企微去找那张卡片

而回一句"同意 12"在**每一条通道上都成立** —— 微信、企微、甚至邮件回信。
按钮那条路以后要加也加得上(卡片的 `EventKey` 里放同一个 id),
但它不该是唯一的一条。

## 三道校验,一道都不能省

1. **签名与时效**:已有的(`wecom_callback.py`)。伪造的回调进不来
2. **这条审批是不是你的**:`approvals` 的每个函数都带 `user_id`(铁律 1)
3. **一次只认一次**:状态机那道 `WHERE status = 'pending'` ——
   企微会对非 200 重投,而**重投两次点两次同意必须只生效一次**

第 3 条是这一片存在的全部理由。少了它,03 那条"零重复执行"就从
"发两条消息"变成"批准两次",而后者更难发现。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from lifein.channels.base import Card, CardSection
from lifein.repos import approvals
from lifein.repos.approvals import Approval, ApprovalStatus

log = logging.getLogger(__name__)

APPROVE_WORDS = ("同意", "批准", "可以", "确认", "ok", "yes")
REJECT_WORDS = ("拒绝", "不同意", "算了", "不用", "no")
LIST_WORDS = ("审批", "待批", "待审批")

_COMMAND = re.compile(r"^\s*(?P<word>\S+?)\s*[#＃]?\s*(?P<id>\d+)\s*$")
""""同意 12""同意12""同意 #12" 都认。

**不认"同意"后面跟一句话**("同意,但改成三点")—— 那种要么是想改内容,
要么是在跟别人说话,而**猜错的代价是发出一条你没同意的消息**。
"""


@dataclass(frozen=True)
class ApprovalReply:
    """处理结果。`card` 不为 None 时由调用方发回去。"""

    handled: bool
    card: Card | None = None
    approval_id: int | None = None
    action: str | None = None


def handle(
    session: Session,
    *,
    user_id: str,
    text: str,
    now: datetime,
) -> ApprovalReply:
    """看这条消息是不是在批审批。**不是就返回 `handled=False`,交给问答。**

    顺序上它排在问答前面:"同意 12" 送进问答 agent 的话,模型会认真地
    去回答"12 是什么",而那既花钱又答非所问。
    """
    body = text.strip()
    if not body:
        return ApprovalReply(handled=False)

    if body in LIST_WORDS:
        return ApprovalReply(handled=True, card=_queue_card(session, user_id, now=now))

    match = _COMMAND.match(body)
    if match is None:
        return ApprovalReply(handled=False)

    word = match.group("word").lower()
    approval_id = int(match.group("id"))

    if any(word.startswith(w) for w in APPROVE_WORDS):
        return _decide(session, user_id, approval_id=approval_id, now=now, approve=True)
    if any(word.startswith(w) for w in REJECT_WORDS):
        return _decide(session, user_id, approval_id=approval_id, now=now, approve=False)
    return ApprovalReply(handled=False)


def _decide(
    session: Session, user_id: str, *, approval_id: int, now: datetime, approve: bool
) -> ApprovalReply:
    before = approvals.get(user_id, session, approval_id=approval_id)
    if before is None:
        # **不区分"不存在"和"不是你的"** —— 区分等于告诉对方这个 id 存在
        # (和 06 §6.13 那条 404 同一个道理)
        return ApprovalReply(
            handled=True,
            card=Card(title="没有这一条", summary=f"#{approval_id} 找不到,或者已经不在了"),
        )

    action = approvals.approve if approve else approvals.reject
    after = action(user_id, session, approval_id=approval_id, now=now)

    if after is None:
        # 没改动。两种原因,**说错了会让人以为自己点漏了**:
        #
        # 一、状态已经变了 —— "一次只认一次"在起作用,不是错误
        #     (企微重投、手机和手表各点一次,都会走到这里)
        # 二、过期了 —— 而这一条**行上的 status 还是 pending**,因为清理 job
        #     可能还没跑到。只看 status 的话会报成"已经处理过了",而那是假话
        if before.status is ApprovalStatus.PENDING and before.expires_at <= now:
            return ApprovalReply(
                handled=True,
                approval_id=approval_id,
                card=Card(
                    title="这条过期了",
                    summary=_already_text("expired"),
                    footer=before.preview_text,
                ),
            )
        return ApprovalReply(
            handled=True,
            approval_id=approval_id,
            card=Card(
                title="这条已经处理过了",
                summary=_already(before),
                footer="重复点一次不会再执行一遍",
            ),
        )

    if approve:
        # **不在这里执行。** 执行由 job 认领 —— 回调有超时,而超时重投会
        # 再执行一次(见 jobs/approval_execute.py 开头那段)
        return ApprovalReply(
            handled=True,
            approval_id=approval_id,
            action="approved",
            card=Card(
                title="好,这就去做",
                summary=after.preview_text,
                footer="做完会再说一声",
            ),
        )
    return ApprovalReply(
        handled=True,
        approval_id=approval_id,
        action="rejected",
        card=Card(title="那就算了", summary=after.preview_text),
    )


def _already(item: Approval) -> str:
    return _already_text(item.status.value)


def _already_text(status: str) -> str:
    return {
        "approved": "已经同意过了,正在做",
        "executed": "已经做完了",
        "rejected": "之前拒绝过",
        "expired": "过期了 —— 超过 24 小时的审批不再生效,免得发出去的东西不合时宜",
        "failed": "做的时候出错了,没有重试",
    }.get(status, f"当前状态:{status}")


def _queue_card(session: Session, user_id: str, *, now: datetime) -> Card:
    """把等着的那些列出来。**每条前面带 id**,因为回复要用它。"""
    items = approvals.list_open(user_id, session, now=now, limit=10)
    if not items:
        return Card(title="没有等你批的", summary="审批队列是空的")

    return Card(
        title=f"有 {len(items)} 条等你批",
        summary="回复“同意 编号”或“拒绝 编号”",
        sections=[
            CardSection(
                heading="等着的",
                lines=[f"#{item.id} {item.preview_text}" for item in items],
            )
        ],
        footer="超过 24 小时会自动过期",
    )
