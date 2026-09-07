"""问答 agent 的测试。

问答比摘要多一样东西:**用户本人的话**。所以除了"答得对不对",还要测
三层结构有没有混 —— 用户的问题必须在隔离标记之外,素材必须在里面。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from lifein.agents.qa import UNGROUNDED_SUFFIX, QaFailed, QaInput, answer, build_blocks
from lifein.llm.client import LLMClient
from lifein.llm.prompt import CLOSE_TAG
from lifein.models.normalized import EventKind, ExternalRef, NormalizedEvent, Trust


def event(external_id: str, *, title="报销单已通过", body="金额 1280 元") -> NormalizedEvent:
    return NormalizedEvent(
        kind=EventKind.MESSAGE,
        title=title,
        occurred_at=datetime(2026, 9, 7, 9, tzinfo=UTC),
        external_ref=ExternalRef(source="email", external_id=external_id),
        trust=Trust.EXTERNAL,
        confidence=1.0,
        body=body,
    )


def capture_llm(payload) -> tuple[LLMClient, list[dict]]:
    sent: list[dict] = []
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    body = {
        "model": "m",
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    return (
        LLMClient(
            base_url="https://llm.example.com/v1",
            api_key="k",
            model="m",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _s: None,
        ),
        sent,
    )


def ask(payload, question="报销批了吗", events=None):
    llm, sent = capture_llm(payload)
    result = answer(
        QaInput(question=question, events=[event("m1")] if events is None else events),
        llm=llm,
    )
    return result, sent


def test_grounded_answer():
    result, _ = ask({"answer": "批了,1280 元。", "refs": ["m1"], "confident": True})
    assert result.output.answer == "批了,1280 元。"
    assert result.output.grounded is True
    assert result.output.refs == ["m1"]


def test_question_lives_outside_the_isolation_markers():
    """用户的问题是指令,素材不是。两者混在一起,注入就有机会伪装成提问。"""
    _, sent = ask({"answer": "a", "refs": ["m1"]}, question="上周我答应了谁什么事")
    body = sent[0]["messages"][1]["content"]
    assert body.index(CLOSE_TAG) < body.index("上周我答应了谁什么事")


def test_ungrounded_answer_is_marked_not_dropped():
    """降级而不是拒答:内容照给,但把"没依据"这件事说出来。"""
    result, _ = ask({"answer": "应该是批了", "refs": [], "confident": True})
    assert result.output.grounded is False
    assert result.output.answer.endswith(UNGROUNDED_SUFFIX)


def test_fabricated_ref_counts_as_ungrounded():
    # 引用了不存在的素材,和没引用是一个性质
    result, _ = ask({"answer": "批了", "refs": ["不存在的id"], "confident": True})
    assert result.output.grounded is False
    assert result.output.refs == []


def test_model_admitting_uncertainty_is_respected():
    # 模型自己说没把握,就不该被当成有依据
    result, _ = ask({"answer": "不太确定", "refs": ["m1"], "confident": False})
    assert result.output.grounded is False


def test_unparseable_response_raises():
    with pytest.raises(QaFailed):
        ask("我觉得应该批了吧")


def test_empty_answer_raises():
    with pytest.raises(QaFailed):
        ask({"answer": "   ", "refs": ["m1"]})


def test_no_events_still_answers():
    # 没有素材时不该崩,该由模型说"素材里没有"
    result, _ = ask({"answer": "素材里没有相关内容", "refs": []}, events=[])
    assert result.output.grounded is False


def test_empty_question_is_rejected_by_the_input_type():
    with pytest.raises(ValueError):
        QaInput(question="  ", events=[])


def test_structured_fields_are_separated_from_body():
    block = build_blocks([event("m1")])[0]
    assert block.fields["标题"] == "报销单已通过"
    assert "报销单已通过" not in block.text


def test_fields_sent_is_reported():
    result, _ = ask({"answer": "a", "refs": ["m1"]})
    assert "body" in result.llm_fields_sent
    assert "标题" in result.llm_fields_sent
