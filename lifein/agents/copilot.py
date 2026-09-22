"""副驾 agent —— 读懂对面那个人,给三条候选回复。

[ADR-035] 开的这条通道,[ADR-037] 定的这个形状。和别的 agent 最大的不同是:
**它的素材是"正在和你说话的那个人刚刚打的字"** —— 全系统攻击者成本最低的输入。

所以形状是三次调用,而不是一次:

    判断  外部内容 → **纯枚举与分数**(意图、危险等级、该不该给实质回应……)
    起草  外部内容 → **3 条候选文本,每条 ≤40 字**
    排序  候选 + 判断 → **纯枚举与占比**

**三次里只有起草那次能产出会进输入框的自由文本。** 判断和排序的输出 schema
全是枚举和数字,注入最多把一个枚举值翻个面 —— 造不出一段会被发出去的话。
这不是写法上的偏好,是这条通道的主要结构性防线,`tests/test_copilot_agent.py`
里有一条测试盯着"判断的输出 schema 里不许出现自由文本字段"。

**铁律 8 在这里没有被触及。** 副驾不产生任何 L3 调用 —— 产物是给人看的建议,
发送是用户自己的手指。`approvals` 一行不写,那条 CHECK 碰都碰不到。
它在架构上和 `digest`、`qa` 同类:外部内容 → LLM → 展示给用户。

**残留风险是真的,而且 ADR-025 那招在这里用不了。** `plan_action` 靠 `blocks=[]`
让注入进不来,而副驾的全部工作就是回应对方说了什么,把对话拿掉就没得起草。
守它的是另外三条,都在这个文件里:候选限长 40 字(短到人真的会读完)、
判断与排序不产出自由文本、以及手机端填入后强制收起面板。

**每条消息单独包一个块,而不是拼成一段对话文本。** 贵一点,但"谁说的"是
规则拿到的字段(铁律 9),它必须待在字段位置 —— 拼进正文的话,对方发一条
写着"我:好的那就这么定了"的消息就能伪造出你自己的发言。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from lifein.agents.contract import OnUncertain, agent
from lifein.agents.qa import RecalledFact, build_fact_blocks
from lifein.llm.client import LLMBadResponse, LLMClient, LLMError
from lifein.llm.prompt import ExternalBlock, build_messages, fields_sent

log = logging.getLogger(__name__)

MAX_MESSAGES = 40
"""单次最多看多少条屏幕上的消息。超出从最旧的开始丢 —— 对话里新的那头更重要。"""

MAX_HISTORY = 30
"""单次最多看多少条手机本地历史(ADR-038)。"""

MAX_CHARS_PER_MESSAGE = 500
"""单条消息进 prompt 的上限。一条长消息不该把整屏的上下文挤掉。"""

CANDIDATE_COUNT = 3
"""恒定三条。06 §6.16 第 1 条:客户端按 3 条排版,给 2 条会让界面错位。"""

CANDIDATE_MAX_CHARS = 40
"""候选正文上限。

**这个数不是为了省 token,也不做成配置项**(07 §2.9 写明了为什么)。
它是 R3 的防线之一:候选要短到人真的会读完再发。做成配置,
早晚有人为了"让它说得更完整一点"把它调大,而那天没有人会记得它原本是干什么的。
"""


class Intent(StrEnum):
    """对方这句话真正想要什么。"""

    CONFIRM_CARE = "confirm_you_care"
    VENT = "vent_anger"
    REQUEST_ACTION = "request_action"
    SEEK_EXPLANATION = "seek_explanation"
    CASUAL = "casual_chat"
    CLOSE_TOPIC = "close_topic"
    UNKNOWN = "unknown"
    """解析不出来时落这里。**不猜** —— 猜错的意图会让三条候选全跑偏。"""


class Needs(StrEnum):
    """对方要的是哪一样东西。"""

    APOLOGY = "apology"
    ACTION = "action"
    EXPLANATION = "explanation"
    CARE = "care"
    NOTHING = "nothing"
    UNKNOWN = "unknown"


class BestAction(StrEnum):
    """这一轮最该做的动作。"""

    CHECK_HISTORY = "check_history"
    APOLOGIZE = "apologize"
    GIVE_COMMITMENT = "give_commitment"
    EXPLAIN = "explain"
    ACKNOWLEDGE = "acknowledge"
    SAY_LESS = "say_less"
    MAKE_PLAN = "make_plan"
    UNKNOWN = "unknown"


class Judgement(BaseModel):
    """判断那一次的输出。

    **这里面不许出现任何自由文本字段。** 全是枚举、布尔和数字 ——
    这是 ADR-037 那条结构性防线的落点,改它之前先读那条 ADR。
    `tests/test_copilot_agent.py::test_judgement_has_no_free_text_field` 盯着它。
    """

    literal: bool = True
    """是不是只有字面意思。false = 有潜台词(试探、反话、暗示不满)。"""

    true_intent: Intent = Intent.UNKNOWN
    intent_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    danger_level: int = Field(default=0, ge=0, le=9)
    """0 = 闲聊,9 = 已经在说断绝关系。分档照搬参考实现,因为它经过真实标注校准。"""

    needs: Needs = Needs.UNKNOWN
    best_action: BestAction = BestAction.UNKNOWN
    should_reply_now: bool = True
    """这一条该不该给出**实质内容**。注意它问的不是"要不要马上回",是"回什么"。"""

    tension_resolved: bool = False


class Candidate(BaseModel):
    text: str = Field(max_length=CANDIDATE_MAX_CHARS)
    rank: int = Field(ge=1)
    share: float = Field(default=0.0, ge=0.0, le=1.0)


class ChatMsg(BaseModel):
    """一条聊天消息。`side` 只认两个值 —— **服务端不猜**(06 §6.16 第 5 条)。"""

    side: str
    text: str = Field(min_length=1)

    @field_validator("side")
    @classmethod
    def _known_side(cls, v: str) -> str:
        if v not in ("me", "other"):
            raise ValueError("side 只能是 me 或 other")
        return v

    @property
    def who(self) -> str:
        return "我" if self.side == "me" else "对方"


class CopilotInput(BaseModel):
    app: str = Field(min_length=1)
    title: str = ""
    """会话标题。用来对 `entity_aliases`,对不上就当陌生人,不报错。"""

    messages: list[ChatMsg] = Field(min_length=1)
    history: list[ChatMsg] = Field(default_factory=list)
    facts: list[RecalledFact] = Field(default_factory=list)
    relationship: str = ""
    """从实体库查到的关系描述。查不到就是空的。"""

    @field_validator("messages")
    @classmethod
    def _latest_exists(cls, v: list[ChatMsg]) -> list[ChatMsg]:
        if not v:
            raise ValueError("messages 不能为空")
        return v

    @property
    def latest_from_other(self) -> bool:
        return self.messages[-1].side == "other"


class CopilotOutput(BaseModel):
    judgement: Judgement
    candidates: list[Candidate] = Field(default_factory=list)
    degraded: str | None = None
    """哪一段降级了:`draft` / `rank` / `quota`。None = 三段都正常。"""


@dataclass(frozen=True)
class CopilotResult:
    output: CopilotOutput
    llm_fields_sent: list[str]
    prompt_tokens: int | None
    completion_tokens: int | None


class CopilotFailed(RuntimeError):
    """判断那一步就没成。悬浮窗要当场说出来,不要静默 —— 用户正等着。"""


_JUDGE_TASK = (
    "你在看一段两个人的聊天。判断**对方最后那句话**的真实意图,"
    "只输出判断结果,不要写任何回复。\n\n"
    "输出严格的 JSON,不要加解释文字,所有字段都必须给:\n"
    "{\n"
    '  "literal": true 或 false,\n'
    '  "true_intent": "confirm_you_care|vent_anger|request_action'
    '|seek_explanation|casual_chat|close_topic",\n'
    '  "intent_confidence": 0 到 1 的小数,\n'
    '  "danger_level": 0 到 9 的整数,\n'
    '  "needs": "apology|action|explanation|care|nothing",\n'
    '  "best_action": "check_history|apologize|give_commitment|explain'
    '|acknowledge|say_less|make_plan",\n'
    '  "should_reply_now": true 或 false,\n'
    '  "tension_resolved": true 或 false\n'
    "}\n\n"
    "字段含义:\n"
    "- literal:false 表示有潜台词(试探、反话、暗示不满)\n"
    "- danger_level:0 是轻松闲聊,3 起有不满,6 起关系有实质风险,9 是已经在说断绝关系\n"
    "- should_reply_now:问的是这一条该不该给出**实质内容**,不是问要不要马上回\n"
    "- tension_resolved:之前的紧张是不是已经缓和了\n\n"
    "**只输出上面这些字段。不要写候选回复,不要写解释。**"
)

_DRAFT_TASK = (
    f"你在帮人回微信。根据下面的聊天和背景,写 {CANDIDATE_COUNT} 条候选回复。\n\n"
    "要求:\n"
    "1. 三条的策略必须不同 —— 一条稳妥接住,一条给具体的行动或承诺,一条简短低姿态\n"
    f"2. **每条不超过 {CANDIDATE_MAX_CHARS} 个字**,口语化,像真人发微信,不要书面语\n"
    "3. 只能用背景里给出的事实,**不要编造背景里没有的事**\n"
    "4. 不要写称呼和落款,直接写要发出去的那句话\n\n"
    "输出严格的 JSON 数组,不要加解释文字:\n"
    '["第一条", "第二条", "第三条"]'
)

_RANK_TASK = (
    "下面是同一个场景的几条候选回复,给它们排序。\n\n"
    "排序标准:\n"
    "1. 更贴合判断出来的「最该做的动作」和「对方要什么」的排前面\n"
    "2. 敷衍、过度承诺、答非所问的排后面\n"
    "3. 背景里没确认的事,宁可选「我去看一下」也不要选假装记得的那条\n\n"
    "输出严格的 JSON,不要加解释文字:\n"
    '{"order": [候选序号从大到小排好, 例如 2, 0, 1], '
    '"shares": [每条对应的合适程度, 0 到 1 的小数, 加起来约等于 1]}\n\n'
    "**order 里放的是候选的序号(从 0 开始),不是候选正文。**"
)


def build_chat_blocks(messages: Sequence[ChatMsg], *, prefix: str) -> list[ExternalBlock]:
    """把消息包成隔离块,**一条一个**。

    "谁说的"走 `fields` 而不是拼进正文:它是规则拿到的字段(铁律 9),
    而且拼进正文的话,对方发一条写着"我:好的那就这么定了"的消息
    就能在模型眼里伪造出你自己的发言。字段位置由 `wrap_external` 自己写,
    正文里再写一遍也盖不掉。
    """
    return [
        ExternalBlock(
            source="chat",
            external_id=f"{prefix}-{i}",
            text=msg.text,
            fields={"谁说的": msg.who},
        )
        for i, msg in enumerate(messages)
    ]


def build_background_blocks(payload: CopilotInput) -> list[ExternalBlock]:
    """关系与记忆。

    事实块直接复用 `qa.build_fact_blocks` —— 它已经处理了"事实也要包隔离标记"
    和"必须带置信度"这两件事,在这里重写一遍只会让两处慢慢跑偏。
    """
    blocks: list[ExternalBlock] = []
    if payload.relationship.strip():
        blocks.append(
            ExternalBlock(
                source="memory",
                external_id="relationship",
                text=payload.relationship.strip(),
                fields={"类型": "关系"},
            )
        )
    blocks.extend(build_fact_blocks(payload.facts))
    return blocks


def judge(blocks: Sequence[ExternalBlock], *, llm: LLMClient) -> tuple[Judgement, tuple]:
    """第一次调用:只出枚举和分数。

    **解析失败要抛。** 和 `plan_recall` 那种"失败就少检索一点"不同,
    判断是这个功能的主体 —— 判断没出来还硬给三条候选,等于在没看懂的情况下
    教人怎么说话。悬浮窗宁可显示"这次没读懂"。
    """
    messages = build_messages(
        task=_JUDGE_TASK, blocks=list(blocks), max_chars_per_block=MAX_CHARS_PER_MESSAGE
    )
    try:
        response = llm.chat(messages)
        parsed = response.as_json()
    except (LLMBadResponse, LLMError) as exc:
        raise CopilotFailed(f"判断失败:{exc}") from exc

    if not isinstance(parsed, dict):
        raise CopilotFailed(f"判断返回的顶层不是对象:{type(parsed).__name__}")

    tokens = (response.prompt_tokens, response.completion_tokens)
    return _parse_judgement(parsed), tokens


def draft(
    chat_blocks: Sequence[ExternalBlock],
    background: Sequence[ExternalBlock],
    *,
    llm: LLMClient,
) -> tuple[list[str], tuple]:
    """第二次调用:唯一一次能产出自由文本的地方。

    **背景块排在对话前面。** 超长被截断时先丢掉的应该是对话的旧消息,
    而不是"这个人是谁"—— 和 `qa.answer` 里那个顺序是同一条道理,方向相反。

    失败返回空列表,不抛:判断已经拿到了,悬浮窗至少能显示"对方想要什么",
    比整个功能挂掉有用。
    """
    messages = build_messages(
        task=_DRAFT_TASK,
        blocks=[*background, *chat_blocks],
        max_chars_per_block=MAX_CHARS_PER_MESSAGE,
    )
    try:
        response = llm.chat(messages)
        parsed = response.as_json()
    except (LLMBadResponse, LLMError) as exc:
        log.info("候选起草失败,这次只给判断:%s", exc)
        return [], (None, None)

    tokens = (response.prompt_tokens, response.completion_tokens)
    if not isinstance(parsed, list):
        log.info("候选返回的顶层不是数组:%s", type(parsed).__name__)
        return [], tokens

    texts: list[str] = []
    for item in parsed:
        text = str(item).strip()
        if text:
            texts.append(text[:CANDIDATE_MAX_CHARS])
    return texts[:CANDIDATE_COUNT], tokens


@dataclass(frozen=True)
class RankResult:
    """排序结果。

    `ok` 单独给,不靠"顺序是不是恰好等于 0,1,2"去猜 —— 模型完全可能
    正常排出一个恒等顺序,那不是降级。猜出来的降级标记会让悬浮窗
    在一切正常的时候显示"排序没成",而那比不显示更糟。
    """

    order: list[int]
    shares: list[float]
    tokens: tuple[int | None, int | None]
    ok: bool


def rank(texts: Sequence[str], judgement: Judgement, *, llm: LLMClient) -> RankResult:
    """第三次调用:也只出序号和占比,不出文本。

    候选正文在这一次里是**素材**,不是指令 —— 所以照样包隔离标记。
    它们本来就是模型上一次写的,但上一次的输入里有对方的话,
    所以这一轮要当外部内容对待。
    """
    if len(texts) < 2:
        # 一条不用排,零条没得排。两种都不算降级
        return RankResult(list(range(len(texts))), _even_shares(len(texts)), (None, None), True)

    blocks = [
        ExternalBlock(
            source="draft",
            external_id=f"cand-{i}",
            text=text,
            fields={"候选序号": str(i)},
        )
        for i, text in enumerate(texts)
    ]
    blocks.append(
        ExternalBlock(
            source="judgement",
            external_id="judgement",
            text=f"最该做的动作:{judgement.best_action}；对方要的是:{judgement.needs}",
            fields={"类型": "判断结果"},
        )
    )

    try:
        response = llm.chat(build_messages(task=_RANK_TASK, blocks=blocks))
        parsed = response.as_json()
    except (LLMBadResponse, LLMError) as exc:
        log.info("候选排序失败,按起草的原顺序用:%s", exc)
        return RankResult(list(range(len(texts))), _even_shares(len(texts)), (None, None), False)

    tokens = (response.prompt_tokens, response.completion_tokens)
    if not isinstance(parsed, dict):
        return RankResult(list(range(len(texts))), _even_shares(len(texts)), tokens, False)

    return RankResult(
        _parse_order(parsed.get("order"), len(texts)),
        _parse_shares(parsed.get("shares"), len(texts)),
        tokens,
        True,
    )


@agent(
    name="copilot",
    inputs=CopilotInput,
    tools=[
        # 三个都是 L1 只读。**这份白名单里一个 L2 / L3 都没有,那是有意的** ——
        # 就算提示注入成功,它能做的最坏的事也只是给出一条烂建议,
        # 而那条建议还要过你的眼睛和你的手指(R3 那节的改判)
        "memory.search_entities",
        "memory.recent_events_with",
        "memory.recall_facts",
    ],
    output_schema=CopilotOutput,
    on_uncertain=OnUncertain.DO_NOTHING,
    evalset="evals/copilot.jsonl",
)
def analyze(payload: CopilotInput, *, llm: LLMClient) -> CopilotResult:
    chat_blocks = build_chat_blocks(payload.messages[-MAX_MESSAGES:], prefix="screen")
    history_blocks = build_chat_blocks(payload.history[-MAX_HISTORY:], prefix="hist")
    background = build_background_blocks(payload)

    judgement, judge_tokens = judge([*history_blocks, *chat_blocks], llm=llm)

    texts, draft_tokens = draft(chat_blocks, [*background, *history_blocks], llm=llm)
    ranked = rank(texts, judgement, llm=llm)

    degraded: str | None = None
    if not texts:
        degraded = "draft"
    elif not ranked.ok:
        degraded = "rank"

    candidates = [
        Candidate(text=texts[idx], rank=position + 1, share=ranked.shares[idx])
        for position, idx in enumerate(ranked.order)
    ]

    all_blocks = [*background, *history_blocks, *chat_blocks]
    return CopilotResult(
        output=CopilotOutput(judgement=judgement, candidates=candidates, degraded=degraded),
        llm_fields_sent=fields_sent(all_blocks),
        prompt_tokens=_sum_tokens(judge_tokens[0], draft_tokens[0], ranked.tokens[0]),
        completion_tokens=_sum_tokens(judge_tokens[1], draft_tokens[1], ranked.tokens[1]),
    )


def _parse_judgement(parsed: dict) -> Judgement:
    """逐字段宽松解析。

    **认不出来的枚举值落 UNKNOWN,不抛。** 模型偶尔会把 `casual_chat` 写成
    `casual`,而为了一个字段让整次分析失败不划算 —— 悬浮窗上少一行,
    比一片空白有用。数值越界就夹回范围,同理。
    """
    return Judgement(
        literal=bool(parsed.get("literal", True)),
        true_intent=_enum_or_unknown(Intent, parsed.get("true_intent")),
        intent_confidence=_clamp(parsed.get("intent_confidence"), 0.0, 1.0),
        danger_level=int(_clamp(parsed.get("danger_level"), 0, 9)),
        needs=_enum_or_unknown(Needs, parsed.get("needs")),
        best_action=_enum_or_unknown(BestAction, parsed.get("best_action")),
        should_reply_now=bool(parsed.get("should_reply_now", True)),
        tension_resolved=bool(parsed.get("tension_resolved", False)),
    )


def _enum_or_unknown(enum_cls: type[StrEnum], value: object) -> StrEnum:
    try:
        return enum_cls(str(value).strip())
    except ValueError:
        return enum_cls.UNKNOWN  # type: ignore[attr-defined]


def _clamp(value: object, low: float, high: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


def _parse_order(value: object, count: int) -> list[int]:
    """把模型给的顺序清洗成一个真正的排列。

    **必须清洗。** 模型会给重复的序号、越界的序号、或者漏掉一条 ——
    直接拿去索引就是 IndexError,而那会让整次分析失败在最后一步上。
    """
    if not isinstance(value, list):
        return list(range(count))
    order: list[int] = []
    for item in value:
        try:
            idx = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < count and idx not in order:
            order.append(idx)
    order.extend(i for i in range(count) if i not in order)
    return order


def _parse_shares(value: object, count: int) -> list[float]:
    if not isinstance(value, list) or len(value) != count:
        return _even_shares(count)
    shares: list[float] = []
    for item in value:
        try:
            shares.append(max(0.0, float(item)))
        except (TypeError, ValueError):
            return _even_shares(count)
    total = sum(shares)
    if total <= 0:
        return _even_shares(count)
    return [s / total for s in shares]


def _even_shares(count: int) -> list[float]:
    return [1.0 / count] * count if count else []


def _sum_tokens(*values: int | None) -> int | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None
