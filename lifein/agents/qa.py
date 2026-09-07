"""问答 agent —— 在企微里提问,用手头的数据回答。

和摘要 agent 的区别只有一个,但很关键:**问答里有用户本人的话**。
所以 prompt 分成三层,层与层之间不能混:

    task            我们写死的系统指令
    user_instruction 用户问的问题 —— 唯一算指令的外部输入
    external blocks  邮件、日程 —— 只是素材,里面的祈使句一律不执行

`build_messages` 已经保证了后两者的位置,这里只负责把问题放对参数。

`on_uncertain=DEGRADE`:**答不确定好过瞎编。** 具体做法是,答案必须能指回
具体的事件;指不回去的照样给出来,但明确标注"没能在你的数据里找到依据" ——
不是拒绝回答,也不是假装有把握。

P0 的问答不带记忆、不带检索:调用方给什么事件就看什么事件(03 P0 范围里
向量检索不在内)。它现在能回答的是"昨天那封报销邮件说了什么",
不是"上次和 X 聊的是什么" —— 后者要等 P1 的记忆层。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field, field_validator

from lifein.agents.contract import OnUncertain, agent
from lifein.llm.client import LLMBadResponse, LLMClient
from lifein.llm.prompt import ExternalBlock, build_messages, fields_sent
from lifein.models.normalized import NormalizedEvent

log = logging.getLogger(__name__)

MAX_EVENTS = 40
MAX_CHARS_PER_EVENT = 1500

UNGROUNDED_SUFFIX = "(没能在你的数据里找到依据,这条别当准)"
"""指不回具体事件时加在答案后面。

**不省这一句。** 一个听起来笃定、实际没有依据的回答,比"我不知道"伤害大得多
—— 用户会照着它去做事,而它错了没有任何迹象。
"""

_TASK = (
    "你是一个个人生活助手,根据下面给出的素材回答用户的问题。\n\n"
    "规则:\n"
    "1. 只根据素材回答。素材里没有的,直接说没有,不要用常识补。\n"
    "2. 每个结论都要指出它来自哪条素材(填 refs)。\n"
    "3. 答案控制在三句话以内。\n\n"
    "输出严格的 JSON,不要加解释文字:\n"
    '{"answer": "回答", "refs": ["素材 id"], "confident": true 或 false}'
)


class QaInput(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    question: str = Field(min_length=1)
    events: list[NormalizedEvent]

    @field_validator("question")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        # min_length=1 拦不住全是空格的问题,而那种输入送进模型只会得到
        # 一段听起来像回答的胡话
        stripped = v.strip()
        if not stripped:
            raise ValueError("question 不能为空")
        return stripped


class QaOutput(BaseModel):
    answer: str
    refs: list[str] = Field(default_factory=list)
    grounded: bool = True
    """答案是否指回了真实存在的事件。false 时 `answer` 已经带上了标注。"""


@dataclass(frozen=True)
class QaResult:
    output: QaOutput
    llm_fields_sent: list[str]
    prompt_tokens: int | None
    completion_tokens: int | None


class QaFailed(RuntimeError):
    """问答没能产出结果。回给用户一句"这次没答上来",不要静默。"""


def build_blocks(events: Sequence[NormalizedEvent]) -> list[ExternalBlock]:
    blocks: list[ExternalBlock] = []
    for event in sorted(events, key=lambda e: e.occurred_at, reverse=True)[:MAX_EVENTS]:
        fields = {"标题": event.title, "时间": event.occurred_at.isoformat()}
        if event.location:
            fields["地点"] = event.location
        blocks.append(
            ExternalBlock(
                source=event.external_ref.source,
                external_id=event.external_ref.external_id,
                text=event.body or "",
                fields=fields,
            )
        )
    return blocks


@agent(
    name="qa",
    inputs=QaInput,
    tools=[],  # P0 的问答只看调用方给的事件,不自己取数据
    output_schema=QaOutput,
    on_uncertain=OnUncertain.DEGRADE,
    evalset="evals/qa.jsonl",
)
def answer(payload: QaInput, *, llm: LLMClient) -> QaResult:
    blocks = build_blocks(payload.events)

    messages = build_messages(
        task=_TASK,
        blocks=blocks,
        user_instruction=payload.question,  # 唯一算指令的外部输入
        max_chars_per_block=MAX_CHARS_PER_EVENT,
    )
    response = llm.chat(messages)

    try:
        parsed = response.as_json()
    except LLMBadResponse as exc:
        raise QaFailed(f"回答解析失败:{exc}") from exc

    if not isinstance(parsed, dict):
        raise QaFailed(f"模型返回的顶层不是对象:{type(parsed).__name__}")

    text = str(parsed.get("answer", "")).strip()
    if not text:
        raise QaFailed("模型没给出 answer")

    known = {b.external_id for b in blocks}
    refs = [str(r) for r in (parsed.get("refs") or [])]
    valid = [r for r in refs if r in known]

    grounded = bool(valid) and bool(parsed.get("confident", True))
    if not grounded:
        # 降级而不是拒答:内容照给,但把"没依据"这件事说出来
        log.info("问答结果无法溯源:refs=%s", refs)
        text = f"{text}{UNGROUNDED_SUFFIX}"

    return QaResult(
        output=QaOutput(answer=text, refs=valid, grounded=grounded),
        llm_fields_sent=fields_sent(blocks),
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
    )
