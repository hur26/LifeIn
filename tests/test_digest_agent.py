"""每日摘要 agent 的测试。

P0 的验收标准是主观的("你觉得有用的比例 > 70%"),所以这里测的不是"摘要好
不好",而是**不好的输出能不能被看出来**:编造的事件要被丢掉且计数,
没有引用的条目要能数出来,整体失败要炸而不是发半截。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import httpx
import pytest

from lifein.agents.digest import (
    MAX_EVENTS,
    DigestCategory,
    DigestFailed,
    DigestInput,
    build_blocks,
    run_digest,
    to_card,
)
from lifein.llm.client import LLMClient
from lifein.models.normalized import (
    EventKind,
    ExternalRef,
    IdentifierType,
    NormalizedEvent,
    Party,
    PartyRole,
    Trust,
)

DAY = date(2026, 9, 7)


def event(external_id: str, *, title: str = "报销单已通过", hour: int = 9) -> NormalizedEvent:
    return NormalizedEvent(
        kind=EventKind.MESSAGE,
        title=title,
        occurred_at=datetime(2026, 9, 7, hour, tzinfo=UTC),
        external_ref=ExternalRef(source="email", external_id=external_id),
        trust=Trust.EXTERNAL,
        confidence=1.0,
        body=f"{title} 的正文",
        parties=[
            Party(
                role=PartyRole.FROM,
                display_name="财务部",
                identifier="finance@example.com",
                identifier_type=IdentifierType.EMAIL,
            )
        ],
    )


def llm_returning(payload) -> LLMClient:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    body = {
        "model": "m",
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    return LLMClient(
        base_url="https://llm.example.com/v1",
        api_key="k",
        model="m",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
        ),
        sleep=lambda _s: None,
    )


def run(payload, events=None):
    # 不用 `events or [...]`:空列表是有意义的输入,不该被默认值顶掉
    return run_digest(
        DigestInput(day=DAY, events=[event("m1")] if events is None else events),
        llm=llm_returning(payload),
    )


def test_normal_digest():
    result = run(
        {
            "summary": "今天有 1 件要紧事。",
            "items": [{"category": "todo", "text": "回复财务的报销邮件", "refs": ["m1"]}],
        }
    )
    out = result.output
    assert out.summary == "今天有 1 件要紧事。"
    assert out.items[0].category is DigestCategory.TODO
    assert out.items[0].unverified is False
    assert out.dropped_hallucinated == 0
    assert out.considered_events == 1


def test_item_citing_a_nonexistent_event_is_dropped():
    """引用了不存在的事件 = 模型编了一封邮件。确定的幻觉。"""
    result = run(
        {
            "summary": "s",
            "items": [
                {"category": "todo", "text": "真的", "refs": ["m1"]},
                {"category": "todo", "text": "编的", "refs": ["不存在的id"]},
            ],
        }
    )
    assert [i.text for i in result.output.items] == ["真的"]
    assert result.output.dropped_hallucinated == 1


def test_item_without_refs_is_kept_but_marked():
    # 可能是几封邮件的综合判断,丢掉太可惜;但要能数出来
    result = run({"summary": "s", "items": [{"category": "fyi", "text": "本周邮件明显变多"}]})
    assert result.output.items[0].unverified is True


def test_partially_valid_refs_keep_only_the_valid_ones():
    result = run(
        {"summary": "s", "items": [{"category": "todo", "text": "t", "refs": ["m1", "假的"]}]}
    )
    assert result.output.items[0].refs == ["m1"]
    assert result.output.items[0].unverified is False


def test_one_malformed_item_does_not_kill_the_digest():
    result = run(
        {
            "summary": "s",
            "items": [
                {"category": "todo", "text": "", "refs": ["m1"]},  # 空文本,不合格
                {"category": "todo", "text": "好的那条", "refs": ["m1"]},
            ],
        }
    )
    assert [i.text for i in result.output.items] == ["好的那条"]
    assert result.output.dropped_hallucinated == 1


def test_unparseable_response_raises_instead_of_half_digest():
    # 每天一条,发一条错的比不发更伤信任(R4)
    with pytest.raises(DigestFailed):
        run("我觉得今天挺忙的")


def test_missing_summary_raises():
    with pytest.raises(DigestFailed):
        run({"items": []})


def test_no_events_raises_rather_than_pushing_an_empty_digest():
    with pytest.raises(DigestFailed):
        run({"summary": "s"}, events=[])


def test_blocks_are_capped_and_newest_first():
    events = [event(f"m{i}", hour=i % 24) for i in range(MAX_EVENTS + 20)]
    blocks = build_blocks(events)
    assert len(blocks) == MAX_EVENTS


def test_structured_fields_are_separated_from_body():
    # 铁律 9:能用规则拿到的字段不进正文
    block = build_blocks([event("m1")])[0]
    assert block.fields["来自"] == "财务部"
    assert "财务部" not in block.text


def test_fields_sent_is_reported_for_the_audit_log():
    # R12:"我到底把什么发给外部供应商了"要能回答
    result = run({"summary": "s"})
    assert "标题" in result.llm_fields_sent
    assert "body" in result.llm_fields_sent


class TestCard:
    def test_categories_become_sections(self):
        result = run(
            {
                "summary": "今天有 2 件事",
                "items": [
                    {"category": "todo", "text": "交房租", "refs": ["m1"]},
                    {"category": "schedule", "text": "15:00 面试", "refs": ["m1"]},
                ],
            }
        )
        card = to_card(result.output, DAY)
        headings = [s.heading for s in card.sections]
        assert headings == ["要做的事", "日程"]
        assert card.title == "9 月 7 日摘要"

    def test_dropped_count_is_visible_to_the_user(self):
        """让质量问题出现在你眼前,而不是藏在日志里。"""
        result = run(
            {"summary": "s", "items": [{"category": "todo", "text": "编的", "refs": ["假"]}]}
        )
        assert "丢弃 1 条" in to_card(result.output, DAY).footer

    def test_empty_category_produces_no_section(self):
        result = run({"summary": "s", "items": [{"category": "todo", "text": "t", "refs": ["m1"]}]})
        card = to_card(result.output, DAY)
        assert len(card.sections) == 1


class TestRefMatching:
    """真跑第一天就撞上的:模型回引 Message-ID 时会去掉尖括号。

    严格比对的结果是所有条目都被当成幻觉丢掉,摘要只剩一句总述 ——
    看起来像模型不好好干活,实际是我们自己把内容扔了。
    """

    def bracketed(self):
        return event("<abc@mail.example.com>")

    def test_ref_without_angle_brackets_still_matches(self):
        result = run(
            {
                "summary": "s",
                "items": [{"category": "todo", "text": "t", "refs": ["abc@mail.example.com"]}],
            },
            events=[self.bracketed()],
        )
        assert result.output.dropped_hallucinated == 0
        assert result.output.items[0].refs == ["<abc@mail.example.com>"]  # 还原成真实 id

    def test_case_and_whitespace_are_tolerated(self):
        result = run(
            {
                "summary": "s",
                "items": [{"category": "todo", "text": "t", "refs": ["  ABC@Mail.Example.Com "]}],
            },
            events=[self.bracketed()],
        )
        assert result.output.items[0].unverified is False

    def test_a_real_hallucination_is_still_dropped(self):
        """放宽只到"明显安全"为止 —— 编出来的 id 照样要被抓住。"""
        result = run(
            {
                "summary": "s",
                "items": [{"category": "todo", "text": "编的", "refs": ["nope@example.com"]}],
            },
            events=[self.bracketed()],
        )
        assert result.output.items == []
        assert result.output.dropped_hallucinated == 1
