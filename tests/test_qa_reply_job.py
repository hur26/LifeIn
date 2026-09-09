"""问答回复的集成测试。

需要真实 PostgreSQL(见 conftest.py)。

重心在**边界**:认不出的人、停用的人、非文字消息、问答失败。正常回答只占
一个用例 —— 它已经在 test_qa_agent.py 里被测透了。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from lifein.channels.base import Card, Delivery, InboundMessage
from lifein.jobs.qa_reply import (
    FAILED_REPLY,
    UNSUPPORTED_REPLY,
    QaDeps,
    default_gateway,
    handle_message,
)
from lifein.llm.client import LLMClient
from lifein.models.normalized import EventKind, ExternalRef, NormalizedEvent, Trust
from lifein.repos import users
from lifein.repos.raw_events import insert_events
from lifein.sources.base import IngestedEvent

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 7, 9, tzinfo=UTC)
WECOM_ID = "BaiYang"


class FakeChannel:
    name = "wecom"

    def __init__(self, boom: Exception | None = None) -> None:
        self.sent: list[Card] = []
        self._boom = boom

    def send(self, user_id, card):
        if self._boom:
            raise self._boom
        self.sent.append(card)
        return Delivery(channel=self.name, delivery_id="msg-1")


def message(content="报销批了吗", msg_type="text", from_user=WECOM_ID) -> InboundMessage:
    return InboundMessage(
        channel="wecom",
        sender=from_user,
        msg_type=msg_type,
        content=content,
        msg_id="1",
        created_at=NOW,
    )


def fake_llm(payload=None) -> LLMClient:
    out = payload or {"answer": "批了,1280 元。", "refs": ["m1"], "confident": True}
    body = {
        "model": "m",
        "choices": [
            {
                "message": {
                    "content": out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                }
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }
    return LLMClient(
        base_url="https://llm.example.com/v1",
        api_key="k",
        model="m",
        max_retries=0,
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
        ),
        sleep=lambda _s: None,
    )


@pytest.fixture
def registered_user(pg_session) -> str:
    return users.create_user(pg_session, display_name="白杨", wecom_userid=WECOM_ID)


def seed_event(pg_session, user_id: str) -> None:
    insert_events(
        user_id,
        pg_session,
        [
            IngestedEvent(
                source="email",
                external_id="m1",
                occurred_at=NOW - timedelta(hours=3),
                trust=Trust.EXTERNAL,
                raw={},
                normalized=NormalizedEvent(
                    kind=EventKind.MESSAGE,
                    title="报销单已通过",
                    occurred_at=NOW - timedelta(hours=3),
                    external_ref=ExternalRef(source="email", external_id="m1"),
                    trust=Trust.EXTERNAL,
                    confidence=1.0,
                    body="金额 1280 元",
                ),
            )
        ],
    )


def resolve_wecom(session, message):
    return users.find_by_wecom_userid(session, wecom_userid=message.sender)


def deps(channel=None, llm=None) -> tuple[QaDeps, FakeChannel]:
    channel = channel or FakeChannel()
    return (
        QaDeps(llm=llm or fake_llm(), channel=channel, resolve_user=resolve_wecom),
        channel,
    )


def test_question_gets_answered(pg_session, registered_user):
    seed_event(pg_session, registered_user)
    d, channel = deps()

    result = handle_message(pg_session, message=message(), deps=d, now=NOW)

    assert result.handled is True
    assert result.user_id == registered_user
    assert channel.sent[0].summary == "批了,1280 元。"
    assert channel.sent[0].title == "报销批了吗"  # 问题放标题,对话流里对得上


def test_unknown_sender_gets_no_reply_at_all(pg_session):
    """回一句"你不是这个系统的用户"等于告诉对方这个地址是活的。"""
    d, channel = deps()
    result = handle_message(pg_session, message=message(from_user="陌生人"), deps=d, now=NOW)

    assert result.handled is False
    assert result.reason == "unknown_user"
    assert channel.sent == []


def test_disabled_user_is_ignored(pg_session, registered_user):
    pg_session.execute(
        text("UPDATE users SET disabled_at = now() WHERE id = :id"), {"id": registered_user}
    )
    d, channel = deps()
    result = handle_message(pg_session, message=message(), deps=d, now=NOW)

    assert result.reason == "disabled"
    assert channel.sent == []


def test_non_text_message_gets_a_plain_explanation(pg_session, registered_user):
    d, channel = deps()
    result = handle_message(pg_session, message=message(msg_type="image"), deps=d, now=NOW)

    assert result.reason == "unsupported_type"
    assert channel.sent[0].summary == UNSUPPORTED_REPLY


def test_qa_failure_replies_in_plain_language(pg_session, registered_user):
    # 用户不需要知道是解析失败还是超时
    seed_event(pg_session, registered_user)
    d, channel = deps(llm=fake_llm("我觉得应该批了吧"))
    result = handle_message(pg_session, message=message(), deps=d, now=NOW)

    assert result.reason == "qa_failed"
    assert channel.sent[0].summary == FAILED_REPLY


def test_reply_is_not_written_to_push_log(pg_session, registered_user):
    """回复不占频率额度 —— 否则多问几句就把当天的摘要额度吃光了。"""
    seed_event(pg_session, registered_user)
    d, _ = deps()
    handle_message(pg_session, message=message(), deps=d, now=NOW)

    assert pg_session.execute(text("SELECT count(*) FROM push_log")).scalar_one() == 0


def test_llm_call_is_audited(pg_session, registered_user):
    seed_event(pg_session, registered_user)
    d, _ = deps()
    handle_message(pg_session, message=message(), deps=d, now=NOW)

    row = pg_session.execute(text("SELECT agent, args_digest FROM tool_calls")).one()
    assert row.agent == "qa"
    # 记的是问题长度不是问题本身
    assert row.args_digest["question_len"] == len("报销批了吗")
    assert "报销批了吗" not in str(row.args_digest)


def test_send_failure_does_not_raise(pg_session, registered_user):
    # 他可以再问一次,不像每日摘要那样"错过就没了"
    seed_event(pg_session, registered_user)
    d, _ = deps(channel=FakeChannel(boom=RuntimeError("企微超时")))
    result = handle_message(pg_session, message=message(), deps=d, now=NOW)
    assert result.handled is True


def test_empty_question_is_dropped(pg_session, registered_user):
    d, channel = deps()
    result = handle_message(pg_session, message=message(content="   "), deps=d, now=NOW)
    assert result.reason == "empty"
    assert channel.sent == []


def test_only_events_inside_the_lookback_window_are_used(pg_session, registered_user):
    seed_event(pg_session, registered_user)
    d, _ = deps()
    handle_message(pg_session, message=message(), deps=d, now=NOW, lookback=timedelta(minutes=1))
    # 事件在 3 小时前,窗口只有 1 分钟 —— 模型拿到的是空素材,
    # 于是 refs 里那个 m1 无效,答案被标成没依据
    row = pg_session.execute(text("SELECT args_digest FROM tool_calls")).scalar_one()
    assert row["events"]["len"] == 0


# ---------- P1:接上记忆之后 ----------


@pytest.fixture(autouse=True)
def _tools_registered():
    # 别的测试文件会 clear_registry() 做隔离,而模块级的 @tool 只在第一次
    # import 时执行 —— 整套跑起来顺序一变,这里就会拿到空注册表
    import importlib

    import lifein.agents.qa as qa_agent
    import lifein.tools.memory as memory_tools
    from lifein.agents import contract
    from lifein.governance import registry

    if "memory.recall_facts" not in registry.registered_tools():
        importlib.reload(memory_tools)
    if "qa" not in contract.registered_agents():
        importlib.reload(qa_agent)


def recording_llm(payloads):
    """和 `scripted_llm` 一样,但把每次发出去的**用户消息原文**留下来。

    ADR-025 那条保证("提议那次调用看不到任何外部素材")只能这样验:
    断言 agent 的输出没用 —— 要看的是**发出去的上下文里到底有什么**。
    """
    seen: list[str] = []
    queue = list(payloads)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        joined = chr(10).join(
            m["content"] for m in body["messages"] if m["role"] == "user"
        )
        seen.append(joined)
        out = queue.pop(0) if queue else {}
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"message": {"content": json.dumps(out, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    return (
        LLMClient(
            base_url="https://llm.example.com/v1",
            api_key="k",
            model="m",
            max_retries=0,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ),
        seen,
    )


def scripted_llm(payloads) -> LLMClient:
    """按顺序回一串响应:第一次是检索线索,第二次才是答案。"""
    queue = list(payloads)

    def handler(request: httpx.Request) -> httpx.Response:
        out = queue.pop(0) if queue else {}
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"message": {"content": json.dumps(out, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    return LLMClient(
        base_url="https://llm.example.com/v1",
        api_key="k",
        model="m",
        max_retries=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _s: None,
    )


def seed_memory(pg_session, user_id: str) -> None:
    """一封七天窗口之外的旧邮件 + 一条记忆里的事实。"""
    from lifein.models.normalized import IdentifierType, Party, PartyRole
    from lifein.repos import facts
    from lifein.repos.entities import AliasType, EntityKind, resolve_or_create

    long_ago = NOW - timedelta(days=90)
    insert_events(
        user_id,
        pg_session,
        [
            IngestedEvent(
                source="email",
                external_id="old-1",
                occurred_at=long_ago,
                trust=Trust.EXTERNAL,
                raw={},
                normalized=NormalizedEvent(
                    kind=EventKind.MESSAGE,
                    title="三季度预算",
                    occurred_at=long_ago,
                    external_ref=ExternalRef(source="email", external_id="old-1"),
                    trust=Trust.EXTERNAL,
                    confidence=1.0,
                    body="预算定在 30 万。",
                    parties=[
                        Party(
                            role=PartyRole.FROM,
                            display_name="张三",
                            identifier="zhang@qq.com",
                            identifier_type=IdentifierType.EMAIL,
                        )
                    ],
                ),
            )
        ],
    )
    resolve_or_create(
        user_id,
        pg_session,
        kind=EntityKind.PERSON,
        name="张三",
        seen_at=long_ago,
        identifier="zhang@qq.com",
        identifier_type=AliasType.EMAIL,
        evidence_event_id=1,
    )
    facts.add_fact(
        user_id,
        pg_session,
        statement="张三负责三季度预算",
        provenance=[1],
        confidence=0.5,
        trust=Trust.EXTERNAL,
        created_by_agent="memory",
        valid_from=long_ago,
    )


def recall_deps(llm, channel=None) -> tuple[QaDeps, FakeChannel]:
    channel = channel or FakeChannel()
    return (
        QaDeps(
            llm=llm,
            channel=channel,
            resolve_user=resolve_wecom,
            gateway_factory=default_gateway,
        ),
        channel,
    )


def tool_rows(pg_session, user_id: str):
    return pg_session.execute(
        text("""
            SELECT tool_name, args_digest FROM tool_calls
             WHERE user_id = :u ORDER BY created_at
        """),
        {"u": user_id},
    ).all()


def test_recall_reaches_events_outside_the_lookback_window(pg_session, registered_user):
    """"上次和张三聊的是什么" —— 那封信在七天窗口之外。

    P1 验收标准第一条就是这个:没有检索时它只能答"最近没有和张三的往来"。
    """
    seed_memory(pg_session, registered_user)
    llm = scripted_llm(
        [
            {"person": "张三", "keywords": ["预算"]},
            {"answer": "上次聊的是三季度预算。", "refs": ["old-1"], "confident": True},
        ]
    )
    d, channel = recall_deps(llm)

    result = handle_message(
        pg_session, message=message("上次和张三聊的是什么"), deps=d, now=NOW
    )

    assert result.handled is True
    assert channel.sent[0].summary == "上次聊的是三季度预算。"
    assert channel.sent[0].footer == "依据 1 条记录"

    names = [row.tool_name for row in tool_rows(pg_session, registered_user)]
    assert "memory.recent_events_with" in names
    assert "memory.recall_facts" in names


def test_both_llm_calls_are_audited(pg_session, registered_user):
    """检索线索那一次也把问句发给了外部供应商,也花了钱(R12)。"""
    seed_memory(pg_session, registered_user)
    llm = scripted_llm([{"person": "张三"}, {"answer": "x", "refs": [], "confident": False}])
    d, _ = recall_deps(llm)

    handle_message(pg_session, message=message("上次和张三聊的是什么"), deps=d, now=NOW)

    stages = [
        row.args_digest.get("stage")
        for row in tool_rows(pg_session, registered_user)
        if row.tool_name == "llm.chat"
    ]
    assert "plan" in stages
    assert len(stages) == 2, "一次问答两次调用,两次都要记"


def test_recall_failure_still_produces_an_answer(pg_session, registered_user):
    """检索炸了只让答案差一点,不该让用户没有回音。"""
    seed_event(pg_session, registered_user)

    def exploding_gateway(user_id, session):
        class Boom:
            # 没接审批队列 —— 这个用例测的是检索,和 L3 那条路无关
            can_approve = False

            def call(self, *_args, **_kwargs):
                raise RuntimeError("记忆层挂了")

        return Boom()

    llm = scripted_llm(
        [{"person": "张三"}, {"answer": "批了,1280 元。", "refs": ["m1"], "confident": True}]
    )
    channel = FakeChannel()
    d = QaDeps(
        llm=llm,
        channel=channel,
        resolve_user=resolve_wecom,
        gateway_factory=exploding_gateway,
    )

    result = handle_message(pg_session, message=message(), deps=d, now=NOW)

    assert result.handled is True
    assert channel.sent[0].summary == "批了,1280 元。"


def test_without_a_gateway_nothing_is_recalled(pg_session, registered_user):
    # 记忆层还没数据的部署照样该能问答,而且不该白花那次检索的钱
    seed_event(pg_session, registered_user)
    d, _ = deps()

    handle_message(pg_session, message=message(), deps=d, now=NOW)

    names = [row.tool_name for row in tool_rows(pg_session, registered_user)]
    assert names == ["llm.chat"], "没有网关时连检索线索那一次都不该调"


# ---------- P3:代发消息(ADR-025) ----------


def approving_deps(llm, *, now=NOW):
    """带审批队列的网关。**没有它 L3 只会被拒**,而那正是这条链路原来的样子。"""
    from lifein.governance.approval_queue import PostgresApprovalQueue
    from lifein.governance.gateway import Gateway
    from lifein.repos.tool_calls import PostgresAuditSink

    channel = FakeChannel()

    def factory(user_id, session):
        return Gateway(
            PostgresAuditSink(user_id, session),
            PostgresApprovalQueue(session, now=now),
        )

    return (
        QaDeps(
            llm=llm,
            channel=channel,
            resolve_user=resolve_wecom,
            gateway_factory=factory,
        ),
        channel,
    )


class TestProposingASend:
    """**这条路原来在生产里一次都不会被触发。**

    `message.send` 注册了、网关会转审批、回调会改状态、执行 job 会去做 ——
    而没有一个 agent 的白名单里有它,所以 `approvals` 永远是空的。
    一段编译得过、测试也绿、而永远不会运行的代码(ADR-025)。
    """

    def test_an_instruction_becomes_an_approval(self, pg_session, registered_user):
        from lifein.repos import approvals

        llm = scripted_llm([{"send": True, "to": "老王", "text": "今晚饭局我去不了了"}])
        d, channel = approving_deps(llm)

        result = handle_message(
            pg_session, message=message("跟老王说今晚饭局我去不了了"), deps=d, now=NOW
        )

        assert result.reason == "approval_required"
        (item,) = approvals.list_open(registered_user, pg_session, now=NOW)
        assert item.tool_name == "message.send"
        assert item.tool_args["text"] == "今晚饭局我去不了了"
        # 卡片上要写清楚**这一次**要做什么,不是"代发一条消息"那种描述
        assert "今晚饭局我去不了了" in channel.sent[-1].summary

    def test_nothing_is_sent_before_you_tap_agree(self, pg_session, registered_user):
        """审批卡片不是消息本身。**这一步一个字都不该发给老王。**"""
        llm = scripted_llm([{"send": True, "to": "老王", "text": "我晚点到"}])
        d, channel = approving_deps(llm)

        handle_message(pg_session, message=message("跟老王说我晚点到"), deps=d, now=NOW)

        # 只有那张"要我替你发出去吗"的卡片,发给用户本人
        assert len(channel.sent) == 1
        assert channel.sent[0].title == "要我替你发出去吗?"

    def test_a_question_is_still_answered(self, pg_session, registered_user):
        """**拿不准一律当提问。** 漏一次他会再说一遍,发错一次撤不回来。"""
        seed_event(pg_session, registered_user)
        llm = scripted_llm(
            [
                {"send": False},
                {"person": ""},
                {"answer": "批了,1280 元。", "refs": ["m1"], "confident": True},
            ]
        )
        d, channel = approving_deps(llm)

        result = handle_message(
            pg_session, message=message("老王上周说了什么"), deps=d, now=NOW
        )

        assert result.handled and result.reason is None
        assert "1280" in channel.sent[-1].summary

    def test_saying_send_without_a_body_proposes_nothing(self, pg_session, registered_user):
        """说要发却没给正文。**不猜** —— 猜出来的那条会被人点同意。"""
        from lifein.repos import approvals

        seed_event(pg_session, registered_user)
        llm = scripted_llm(
            [
                {"send": True, "to": "老王", "text": "   "},
                {"person": ""},
                {"answer": "不知道", "refs": [], "confident": False},
            ]
        )
        d, _ = approving_deps(llm)

        handle_message(pg_session, message=message("跟老王说"), deps=d, now=NOW)
        assert approvals.list_open(registered_user, pg_session, now=NOW) == []

    def test_the_proposal_never_sees_external_material(self, pg_session, registered_user):
        """**ADR-025 的那条结构性保证。**

        网关那道 `trust is user_input` 挡不住"检索回来的邮件里写着一句指令"
        —— 问句确实是用户打的。挡住它的是:提议那次调用的上下文里
        **一个外部素材块都没有**。

        这里直接看发出去的第一条请求:里面只能有用户那句话,
        不能出现任何被隔离标记包起来的素材。
        """
        seed_memory(pg_session, registered_user)
        llm, seen = recording_llm([{"send": False}, {"person": ""}, {"answer": "x", "refs": []}])
        d, _ = approving_deps(llm)

        handle_message(
            pg_session, message=message("上次和张三聊的是什么"), deps=d, now=NOW
        )

        first = seen[0]
        assert "上次和张三聊的是什么" in first
        assert "<external" not in first, "提议那次调用不许带任何外部素材(ADR-025)"

    def test_without_an_approval_queue_it_stays_a_question(self, pg_session, registered_user):
        """P0/P1 那种没接审批队列的部署。**不是错误** —— L3 本来就还没上线。

        而且那时不该白花一次模型调用:第一次请求就该是检索线索,不是代发判断。
        """
        seed_event(pg_session, registered_user)
        llm, seen = recording_llm(
            [{"person": ""}, {"answer": "批了。", "refs": ["m1"], "confident": True}]
        )
        d, _ = recall_deps(llm)

        result = handle_message(
            pg_session, message=message("跟老王说我晚点到"), deps=d, now=NOW
        )

        assert result.handled and result.reason is None
        assert len(seen) == 2, "没接审批队列时不该多花一次调用去判代发"
