"""记忆抽取 agent 的测试。

两半分开测,因为它们的失败方式完全不同:

- **实体那一半不调模型**,所以测的是规则本身对不对(自己要被排除、
  卡号后四位不能当别名、同一封信里出现两次算一次)
- **事实那一半调模型**,所以测的是"模型胡说时会不会被写进记忆" ——
  指不回事件的一律丢,这条比摘要严,因为记忆会被反复引用
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from lifein.agents.memory import (
    STATEMENT_MAX,
    MemoryFailed,
    MemoryInput,
    build_blocks,
    extract,
    sightings_from,
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
from lifein.repos.entities import AliasType, EntityKind
from lifein.repos.raw_events import StoredEvent

NOW = datetime(2026, 9, 8, 9, tzinfo=UTC)


def party(role: PartyRole, name: str, ident: str | None = None, kind=IdentifierType.EMAIL):
    return Party(
        role=role,
        display_name=name,
        identifier=ident,
        identifier_type=kind if ident else None,
    )


def stored(
    event_id: int,
    external_id: str = "m1",
    *,
    parties=None,
    body: str = "麻烦你周三前把报销单交给我。",
    trust: Trust = Trust.EXTERNAL,
) -> StoredEvent:
    return StoredEvent(
        event_id=event_id,
        event=NormalizedEvent(
            kind=EventKind.MESSAGE,
            title="报销单",
            occurred_at=NOW,
            external_ref=ExternalRef(source="email", external_id=external_id),
            trust=trust,
            confidence=1.0,
            body=body,
            parties=parties
            if parties is not None
            else [party(PartyRole.FROM, "张三", "Zhang@QQ.com")],
        ),
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


def run(payload, events=None, own=()):
    return extract(
        MemoryInput(
            events=[stored(1)] if events is None else events,
            own_identifiers=list(own),
        ),
        llm=llm_returning(payload),
    )


# ---------- 实体:纯规则那一半 ----------


def test_sightings_come_from_parties_without_the_model():
    sightings = sightings_from([stored(1)])
    assert len(sightings) == 1
    assert sightings[0].name == "张三"
    assert sightings[0].kind is EntityKind.PERSON
    # 原样交给仓储,归一化在那边做 —— 归一化规则只该有一个出处
    assert sightings[0].identifier == "Zhang@QQ.com"
    assert sightings[0].identifier_type is AliasType.EMAIL
    assert sightings[0].event_id == 1


def test_user_themselves_is_excluded():
    """不排除自己,记忆里很快会多出一个叫你自己的实体。

    然后"我这周答应了谁什么事"会答出你答应了你自己。
    """
    events = [
        stored(
            1,
            parties=[
                party(PartyRole.FROM, "张三", "zhang@qq.com"),
                party(PartyRole.TO, "我", "ME@example.com"),
            ],
        )
    ]
    names = [s.name for s in sightings_from(events, own_identifiers=["me@example.com"])]
    assert names == ["张三"]


def test_card_last4_is_dropped_but_the_name_survives():
    card = party(PartyRole.MERCHANT, "招商银行", "1234", IdentifierType.CARD_LAST4)
    events = [stored(1, parties=[card])]
    sighting = sightings_from(events)[0]
    assert sighting.kind is EntityKind.MERCHANT
    assert sighting.name == "招商银行"
    assert sighting.identifier is None, "卡号标识的是一张卡,不是一个人"


def test_nameless_party_falls_back_to_its_identifier():
    # 邮件里 `<a@b.com>` 不带姓名很常见,那时邮箱就是我们对这个人唯一的称呼
    events = [stored(1, parties=[party(PartyRole.FROM, "  ", "a@b.com")])]
    assert sightings_from(events)[0].name == "a@b.com"


def test_party_without_name_or_identifier_is_skipped():
    events = [stored(1, parties=[party(PartyRole.CC, "   ")])]
    assert sightings_from(events) == []


def test_same_person_twice_in_one_event_counts_once():
    events = [
        stored(
            1,
            parties=[
                party(PartyRole.TO, "张三", "zhang@qq.com"),
                party(PartyRole.CC, "张三", "zhang@qq.com"),
            ],
        )
    ]
    assert len(sightings_from(events)) == 1


def test_same_person_in_two_events_counts_twice():
    # 两条独立证据,别名置信度该因此上涨 —— 去重只在单条事件内
    events = [stored(1, "m1"), stored(2, "m2")]
    assert len(sightings_from(events)) == 2


# ---------- 事实:模型那一半 ----------


def test_grounded_fact_is_kept_with_provenance():
    result = run({"facts": [{"statement": "张三在市场部", "refs": ["m1"], "confidence": 0.8}]})
    fact = result.output.facts[0]
    assert fact.statement == "张三在市场部"
    assert fact.provenance == [1], "provenance 是 raw_events.id,不是模型看到的 external_id"
    assert fact.confidence == pytest.approx(0.8)
    assert result.output.dropped_ungrounded == 0


def test_fact_without_refs_is_dropped():
    """摘要保留无引用条目并标 unverified,记忆不行。

    铁律 5:没有来源的记忆不许落库。这里丢掉,库里那条 CHECK 就永远用不上 ——
    那才是它该有的样子。
    """
    result = run({"facts": [{"statement": "张三喜欢喝茶", "refs": [], "confidence": 0.9}]})
    assert result.output.facts == []
    assert result.output.dropped_ungrounded == 1


def test_fact_referencing_an_unknown_event_is_dropped():
    # 引用了没给过它的素材 = 模型编了一封邮件
    result = run(
        {"facts": [{"statement": "李四要离职", "refs": ["不存在的信"], "confidence": 0.9}]}
    )
    assert result.output.facts == []
    assert result.output.dropped_ungrounded == 1


def test_refs_are_matched_after_normalization():
    # 邮件的 external_id 是带尖括号的 Message-ID,模型回引时几乎总会去掉
    events = [stored(7, "<abc@mail.qq.com>")]
    result = run(
        {"facts": [{"statement": "张三在市场部", "refs": ["ABC@mail.qq.com"], "confidence": 0.5}]},
        events=events,
    )
    assert result.output.facts[0].provenance == [7]


def test_trust_takes_the_least_trustworthy_source():
    """一条依据是外部的,整条就是外部的。

    仓储据此把置信度封到 0.6。取最不可信的那条是唯一安全的取法 ——
    反过来会让一句"用户自己说的"给整条事实背书。
    """
    events = [stored(1, "m1", trust=Trust.USER_INPUT), stored(2, "m2", trust=Trust.EXTERNAL)]
    result = run(
        {"facts": [{"statement": "我答应张三周三交报销", "refs": ["m1", "m2"], "confidence": 0.9}]},
        events=events,
    )
    assert result.output.facts[0].trust is Trust.EXTERNAL
    assert result.output.facts[0].provenance == [1, 2]


def test_all_user_input_stays_user_input():
    events = [stored(1, "m1", trust=Trust.USER_INPUT)]
    result = run(
        {"facts": [{"statement": "我答应张三周三交报销", "refs": ["m1"], "confidence": 0.9}]},
        events=events,
    )
    assert result.output.facts[0].trust is Trust.USER_INPUT


def test_overlong_statement_is_dropped():
    # 超长的多半是模型把整封邮件复述了一遍,那种"事实"没法被否定也没法被检索
    result = run(
        {"facts": [{"statement": "张" * (STATEMENT_MAX + 1), "refs": ["m1"], "confidence": 0.5}]}
    )
    assert result.output.facts == []
    assert result.output.dropped_ungrounded == 1


def test_out_of_range_confidence_is_clamped_not_rejected():
    result = run({"facts": [{"statement": "张三在市场部", "refs": ["m1"], "confidence": 1.7}]})
    assert result.output.facts[0].confidence == pytest.approx(1.0)


def test_missing_confidence_falls_back_to_half():
    result = run({"facts": [{"statement": "张三在市场部", "refs": ["m1"]}]})
    assert result.output.facts[0].confidence == pytest.approx(0.5)


def test_no_events_means_no_model_call():
    """没素材就别调模型 —— 对着空 prompt 抽出来的事实一定是编的。"""

    def explode(_messages):
        raise AssertionError("不该调模型")

    class Boom:
        chat = staticmethod(explode)

    result = extract(MemoryInput(events=[]), llm=Boom())
    assert result.output.facts == []
    assert result.output.sightings == []
    assert result.prompt_tokens is None


def test_broken_json_fails_loudly():
    # 抽取整体失败要炸,让调度层告警;静默跳过等于记忆悄悄停止更新
    with pytest.raises(MemoryFailed):
        run("这不是 JSON")


def test_top_level_not_an_object_fails():
    with pytest.raises(MemoryFailed):
        run(["facts"])


def test_blocks_carry_rule_extracted_fields_not_body_only():
    """标题、时间、发件人用规则单独给(铁律 9 + R12)。

    它们会原样离开自托管环境到达外部供应商,所以要能逐字段回答
    "我到底发出去了什么"。
    """
    block = build_blocks([stored(1)])[0]
    assert set(block.fields) == {"标题", "时间", "发件人"}
    assert block.fields["发件人"] == "张三"
    assert block.text == "麻烦你周三前把报销单交给我。"
