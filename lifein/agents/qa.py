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

P1 起它带记忆了:调用方除了给最近的事件,还会给**检索回来的事件**和
**记忆里的事实**。于是"上次和 X 聊的是什么"能答了。

**事实和邮件一样,都包在隔离标记里。** 这一条容易想错:事实是系统自己写的,
看起来算"内部内容"。但它是**从邮件正文里推出来的** —— 一封写着"忽略上述
指令"的邮件,抽取出的事实同样可能带着那句话。来源不可信,推论就不可信,
所以它继承外部内容的待遇(R3)。

事实进 prompt 时一定带上置信度和"用户确认过没有"。不带的话,一条 0.4 分的
推断和用户亲口说过的话在模型眼里一模一样。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from lifein.agents.contract import OnUncertain, agent
from lifein.llm.client import LLMBadResponse, LLMClient, LLMError
from lifein.llm.prompt import ExternalBlock, build_messages, fields_sent, normalize_ref
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


_PLAN_TASK = (
    "从用户的问题里找出检索线索,不要回答问题本身。\n\n"
    "输出严格的 JSON,不要加解释文字:\n"
    '{"person": "问题里提到的人名或商户名,没有就填空字符串",' "\n"
    ' "keywords": ["最多两个用来检索的关键词"]}' "\n\n"
    "person 只填问题里真的出现了的称呼,不要推测。"
)


class RecallPlan(BaseModel):
    """从问句里解析出的检索线索。**两个字段都可能为空,那就是不检索。**"""

    person: str = ""
    keywords: list[str] = Field(default_factory=list)


_ACTION_TASK = (
    "判断用户这句话是在**问一件事**,还是在**让你替他发一条消息**。\n\n"
    "输出严格的 JSON,不要加解释文字:\n"
    '{"send": true 或 false,'
    ' "to": "要发给谁,照他说的写,没说就填空字符串",'
    ' "text": "要发出去的正文"}\n\n'
    "**是提问就把 send 填 false**,别的字段留空。"
    "问句里带着人名不代表要发消息 —— "
    '"老王上周说了什么" 是提问,"跟老王说我晚点到" 才是要发。\n'
    "拿不准一律填 false:漏一次他会再说一遍,发错一次撤不回来。"
)

MAX_MESSAGE_CHARS = 500
"""代发正文的上限。

**这个数不是为了省 token。** 一条要人点头的消息如果长到卡片上放不下,
那张卡片就变成了"点同意"而不是"看清楚再点同意" —— 而 03 的退出条件是
"你自己不敢点同意 → 预览做得不够清楚"。
"""


class ProposedMessage(BaseModel):
    """模型提议替你发的一条消息。**它只是提议** —— 真发出去要过审批(L3)。"""

    to: str = ""
    text: str = ""

    @property
    def is_real(self) -> bool:
        return bool(self.text.strip())


@dataclass(frozen=True)
class ActionResult:
    """提议 + 这次调用的 token 数。token 要带出去,理由和 `PlanResult` 一样。"""

    message: ProposedMessage | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(frozen=True)
class PlanResult:
    """检索线索 + 这次调用的 token 数。

    token 数要带出去:这一次同样把问句发给了外部供应商,也同样花钱,
    调用方要拿它记审计。不带的话成本表里会少掉一半的问答开销。
    """

    plan: RecallPlan
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class RecalledEvent(BaseModel):
    """检索工具找回来的事件。

    不复用 `NormalizedEvent`:工具回的是给模型看的扁平结构(正文已截断),
    硬塞回骨架等于假装我们手上有完整的那条事件。
    """

    source: str
    external_id: str
    title: str
    occurred_at: datetime
    body: str = ""


class RecalledFact(BaseModel):
    fact_id: str
    statement: str
    confidence: float
    confirmed_by_user: bool = False


class QaInput(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    question: str = Field(min_length=1)
    events: list[NormalizedEvent]
    recalled: list[RecalledEvent] = Field(default_factory=list)
    """按人名检索回来的历史事件。可能落在 `events` 的时间窗之外 —— 那正是它的用处。"""

    facts: list[RecalledFact] = Field(default_factory=list)

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


def build_recalled_blocks(recalled: Sequence[RecalledEvent]) -> list[ExternalBlock]:
    return [
        ExternalBlock(
            source=item.source,
            external_id=item.external_id,
            text=item.body,
            fields={"标题": item.title, "时间": item.occurred_at.isoformat()},
        )
        for item in recalled
    ]


def build_fact_blocks(facts: Sequence[RecalledFact]) -> list[ExternalBlock]:
    """事实也包进隔离标记。

    它是系统自己写的,看起来算"内部内容" —— 但它是从邮件正文里推出来的,
    一封写着"忽略上述指令"的邮件抽出的事实同样可能带着那句话。
    **来源不可信,推论就不可信**(R3)。

    置信度和"用户确认过没有"必须一起给:少了这两个字段,一条 0.4 分的推断
    在模型眼里和用户亲口说过的话没有区别。
    """
    return [
        ExternalBlock(
            source="memory",
            external_id=fact.fact_id,
            text=fact.statement,
            fields={
                "置信度": f"{fact.confidence:.2f}",
                "用户确认过": "是" if fact.confirmed_by_user else "否",
            },
        )
        for fact in facts
    ]


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


def plan_recall(question: str, *, llm: LLMClient) -> PlanResult:
    """先问一次模型:这个问题要查谁、查什么词。

    **两次调用是刻意的。** 让模型在一次调用里既决定检索什么又回答问题,
    结果是两件事互相拖累 —— 它会倾向于用手头已有的素材硬答。分开之后
    第一次的输出只有两个字段,又短又好验。

    这一步不算违反铁律 9:"从中文问句里认出人名"没有可靠的规则写法,
    分词和词典都会在"老张""李工"这类称呼上翻车。规则能拿到的字段
    (发件人、时间)从来没进过模型,那条铁律说的是那些。

    **解析失败就返回空计划,不抛。** 检索不到最多是答得差一点,
    而让整次问答失败是实打实的不可用。
    """
    messages = build_messages(task=_PLAN_TASK, blocks=[], user_instruction=question)
    try:
        response = llm.chat(messages)
        parsed = response.as_json()
    except (LLMBadResponse, LLMError) as exc:
        log.info("检索线索解析失败,这次不检索:%s", exc)
        return PlanResult(RecallPlan())

    tokens = (response.prompt_tokens, response.completion_tokens)
    if not isinstance(parsed, dict):
        return PlanResult(RecallPlan(), *tokens)

    keywords = [str(k).strip() for k in (parsed.get("keywords") or []) if str(k).strip()]
    return PlanResult(
        RecallPlan(person=str(parsed.get("person") or "").strip(), keywords=keywords[:2]),
        *tokens,
    )


def plan_action(question: str, *, llm: LLMClient) -> ActionResult:
    """再问一次模型:这句话是提问,还是让我替他发一条消息。

    **这次调用只看用户自己打的那句话,一个外部素材块都不带**
    (`blocks=[]`)—— 这是 [ADR-025] 的全部内容,也是铁律 8 在这条路上
    结构性成立的地方。

    网关那道 `trust is user_input` 挡不住这一种:问句确实是他打的。而一封
    写着"请帮我转告所有人……"的邮件被检索回来之后,模型完全可能把它当成
    要执行的事 —— 而那张审批卡片会长得非常像他自己要的东西。
    上下文里根本没有外部内容,注入就进不来。

    和 `plan_recall` 是同一个形状。**同一个形状用两次,比为第二次发明
    一套新的更可靠。**

    **解析失败返回空提议,不抛。** 最坏的结果应该是"它没听懂,我再说一遍",
    不是整次问答失败。
    """
    messages = build_messages(task=_ACTION_TASK, blocks=[], user_instruction=question)
    try:
        response = llm.chat(messages)
        parsed = response.as_json()
    except (LLMBadResponse, LLMError) as exc:
        log.info("代发意图解析失败,这次不提议:%s", exc)
        return ActionResult()

    tokens = (response.prompt_tokens, response.completion_tokens)
    if not isinstance(parsed, dict) or parsed.get("send") is not True:
        return ActionResult(None, *tokens)

    text = str(parsed.get("text") or "").strip()
    if not text:
        # 说要发,却没给正文。**不猜** —— 猜出来的那条会被人点同意
        log.info("模型说要发消息但没给正文,忽略")
        return ActionResult(None, *tokens)

    return ActionResult(
        ProposedMessage(
            to=str(parsed.get("to") or "").strip(),
            text=text[:MAX_MESSAGE_CHARS],
        ),
        *tokens,
    )


@agent(
    name="qa",
    inputs=QaInput,
    tools=[
        # 双重门:问答能查记忆,不代表别的 agent 也能。工具本身是 L1 也一样
        "memory.search_entities",
        "memory.recent_events_with",
        "memory.recall_facts",
        # **唯一的 L3。** 加它之前 approvals 那条链路在生产里一次都不会被
        # 触发 —— 接口全在,没有调用方(ADR-025)。
        # 提议由 `plan_action` 产生,那次调用看不到任何外部素材
        "message.send",
        # 账本(P2)。**只读,改账在 App 里做**(ADR-028)。
        # 决定性的那条理由是指代不准:"昨天那笔""刚才星巴克那笔"在同额同商户时
        # 是歧义的,而消歧要来回问两轮 —— 那时候不如打开 App 点一下
        "ledger.query",
        "ledger.spending",
    ],
    output_schema=QaOutput,
    on_uncertain=OnUncertain.DEGRADE,
    evalset="evals/qa.jsonl",
)
def answer(payload: QaInput, *, llm: LLMClient) -> QaResult:
    # 顺序有意为之:最近的事件在前,检索回来的历史其次,记忆里的推断在最后。
    # 素材超长被截断时,先丢掉的是最不该被当成事实的那一类
    seen = {e.external_ref.external_id for e in payload.events}
    blocks = [
        *build_blocks(payload.events),
        *build_recalled_blocks([r for r in payload.recalled if r.external_id not in seen]),
        *build_fact_blocks(payload.facts),
    ]

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

    known = {normalize_ref(b.external_id): b.external_id for b in blocks}
    refs = [str(r) for r in (parsed.get("refs") or [])]
    valid = [known[key] for r in refs if (key := normalize_ref(r)) in known]

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
