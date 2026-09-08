"""回答企微里发来的提问。

和每日摘要的区别是**它由用户触发**,这带来两条不同的规矩:

**不写 `push_log`。** 那张表是频率闸门的计数来源,而闸门管的是"非用户主动
触发的推送每天不超过 3 条"(产品定义 §5)。把回复记进去,等于你多问几句
就把自己当天的摘要额度吃光了。

**认不出的人直接丢,连回复都不给。** 企业里任何人都能给自建应用发消息。
回一句"你不是这个系统的用户"等于告诉对方这个地址是活的、后面有东西 ——
不回复才是正确的沉默。

这里没有把用户的提问当成指令去执行任何操作:P0 的问答只读、只回话。
提问本身进 prompt 的位置由 `build_messages` 保证(qa.py 里说明了三层结构)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from lifein.agents.qa import QaFailed, QaInput, answer
from lifein.channels.base import Card, Channel, InboundMessage
from lifein.governance.audit import ToolCallRecord
from lifein.governance.registry import ToolLevel
from lifein.llm.client import LLMClient, LLMError
from lifein.repos import raw_events, users
from lifein.repos.tool_calls import record_tool_call

log = logging.getLogger(__name__)

LOOKBACK = timedelta(days=7)
"""问答能看多久的事件。

P0 没有检索,只能把一段时间的事件整批喂进去,所以这个窗口既是能力上限也是
成本上限。七天是个折中:再长就装不下,再短连"上周"都答不了。
P1 有了向量检索之后这个常量就该消失。
"""

UNSUPPORTED_REPLY = "我这边只认文字消息,图片和语音还看不了。"
FAILED_REPLY = "这次没答上来,过会儿再问我一次。"

MAX_QUESTION_CHARS = 500


@dataclass(frozen=True)
class QaDeps:
    llm: LLMClient
    channel: Channel
    resolve_user: Callable[[Session, InboundMessage], users.User | None]
    """把"通道内的发送者"换成本系统的用户。

    企微认 UserID、iLink 认对方的 user id,**这个映射是通道的事,不是问答的事**。
    注入进来,问答就不用为每加一个入站通道改一次。
    """


@dataclass
class ReplyResult:
    handled: bool
    user_id: str | None = None
    reason: str | None = None
    """没处理的原因。unknown_user / disabled / unsupported_type / empty。"""


def handle_message(
    session: Session,
    *,
    message: InboundMessage,
    deps: QaDeps,
    now: datetime,
    lookback: timedelta = LOOKBACK,
) -> ReplyResult:
    user = deps.resolve_user(session, message)
    if user is None:
        # 陌生人。不回复 —— 回复等于告诉对方这个地址是活的
        log.warning("收到未知用户的消息:channel=%s sender=%s", message.channel, message.sender)
        return ReplyResult(handled=False, reason="unknown_user")

    if not user.active:
        log.info("用户 %s 已停用,忽略消息", user.id)
        return ReplyResult(handled=False, user_id=user.id, reason="disabled")

    if message.msg_type != "text":
        _reply(deps, user.id, message, UNSUPPORTED_REPLY)
        return ReplyResult(handled=False, user_id=user.id, reason="unsupported_type")

    question = message.content.strip()[:MAX_QUESTION_CHARS]
    if not question:
        return ReplyResult(handled=False, user_id=user.id, reason="empty")

    events = raw_events.fetch_normalized_between(user.id, session, start=now - lookback, end=now)

    try:
        result = answer(QaInput(question=question, events=events), llm=deps.llm)
    except (QaFailed, LLMError) as exc:
        # 回一句人话,不回堆栈。用户不需要知道是解析失败还是超时
        log.warning("问答失败:%s", exc)
        _reply(deps, user.id, message, FAILED_REPLY)
        return ReplyResult(handled=False, user_id=user.id, reason="qa_failed")

    record_tool_call(
        user.id,
        session,
        ToolCallRecord(
            user_id=user.id,
            agent="qa",
            tool_name="llm.chat",
            level=ToolLevel.L1,
            args_digest={
                "question_len": len(question),
                "events": {"type": "list", "len": len(events)},
            },
            llm_fields_sent=result.llm_fields_sent,
            result_status="allowed",
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        ),
    )

    _reply(deps, user.id, message, result.output.answer, refs=result.output.refs)
    return ReplyResult(handled=True, user_id=user.id)


def _reply(
    deps: QaDeps,
    user_id: str,
    message: InboundMessage,
    text: str,
    refs: Sequence[str] = (),
) -> None:
    card = Card(
        # 把问题放标题里:企微的对话流里,回复和提问之间可能隔了别的消息
        title=message.content.strip()[:40] or "回答",
        summary=text,
        footer=f"依据 {len(refs)} 条记录" if refs else None,
    )
    try:
        deps.channel.send(user_id, card)
    except Exception:  # noqa: BLE001
        # 回不出去就算了:用户还在那儿等着,但重试也多半失败,
        # 而这条不像每日摘要那样"错过就没了"—— 他可以再问一次
        log.exception("回复发送失败,user=%s", user_id)
