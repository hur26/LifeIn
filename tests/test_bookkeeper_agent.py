"""记账 agent:四层防误判的第 3、4 层(P2 第 3 片)。不需要库,用假 LLM。

**03 给这个 agent 的验收标准不设百分比**:

> 误记率 = 0:一个月内没有任何一笔"待支付提醒""信用卡还款""退款"
> 被错记成支出。

所以这一组几乎全在测**没入账**的那些路径:模型答错了拦不拦得住、
拿不准时会不会硬塞一个、以及金额被改过能不能发现。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lifein.agents.bookkeeper import (
    CATEGORIES,
    BookkeeperInput,
    Judgment,
    Route,
    judge,
)
from lifein.models.normalized import (
    Amount,
    Direction,
    EventKind,
    ExternalRef,
    NormalizedEvent,
    Trust,
)
from lifein.repos.raw_events import StoredEvent
from lifein.repos.transactions import TxnKind

NOW = datetime(2026, 9, 8, 20, 15, tzinfo=UTC)


class FakeLLM:
    """按脚本回答。**测的是我们怎么对待模型的答案**,不是模型本身。"""

    def __init__(self, items: list[dict] | str) -> None:
        self.payload = items
        self.seen: list = []

    def chat(self, messages):
        self.seen.append(messages)
        content = self.payload if isinstance(self.payload, str) else json.dumps(
            {"items": self.payload}, ensure_ascii=False
        )
        return _Response(content)


class _Response:
    def __init__(self, content: str) -> None:
        self._content = content
        self.prompt_tokens = 100
        self.completion_tokens = 20

    def as_json(self):
        return json.loads(self._content)


def an_event(
    event_id: int = 1,
    *,
    amount: str = "38.50",
    body: str = "您尾号1234的卡9月8日20:15消费人民币38.50元",
    external_id: str = "pixel:t-1",
    direction: Direction = Direction.DEBIT,
    matched: str | None = None,
) -> StoredEvent:
    return StoredEvent(
        event_id=event_id,
        raw={"parsed": {"matched": matched}} if matched else None,
        event=NormalizedEvent(
            kind=EventKind.TRANSACTION,
            title="招商银行",
            occurred_at=NOW,
            external_ref=ExternalRef(source="notification", external_id=external_id),
            trust=Trust.EXTERNAL,
            confidence=1.0,
            amount=Amount(value=Decimal(amount), currency="CNY", direction=direction),
            body=body,
        ),
    )


def run(items, events=None, *, min_confidence: float = 0.8):
    llm = FakeLLM(items)
    result = judge(
        BookkeeperInput(events=events or [an_event()], min_confidence=min_confidence), llm=llm
    )
    return result.output, llm


class TestWhatMustNotBeRecorded:
    """03 那条"误记率 = 0"对应的路径。"""

    def test_a_repayment_is_never_an_expense(self):
        """信用卡还款记成支出就是双重记账 —— 消费那一刻已经记过一次。"""
        output, _ = run([{"ref": "pixel:t-1", "judgment": "repayment", "confidence": 0.95}])

        (item,) = output.items
        assert item.kind is TxnKind.REPAYMENT
        assert item.route is Route.DIRECT  # 入账,但 kind 不是 expense
        assert not item.kind.counts_as_spending

    def test_marketing_is_discarded_not_queued(self):
        """营销每天都有,进队列会把队列淹掉,而淹掉的队列等于没有队列。"""
        output, _ = run([{"ref": "pixel:t-1", "judgment": "marketing", "confidence": 0.95}])

        (item,) = output.items
        assert item.route is Route.DISCARD
        assert item.kind is None

    def test_pending_payment_is_not_money_moving(self):
        output, _ = run([{"ref": "pixel:t-1", "judgment": "pending_payment", "confidence": 0.9}])
        assert output.items[0].kind is None

    def test_an_unsure_marketing_call_still_goes_to_the_queue(self):
        """模型自己都不确定是不是营销 —— 那就交给人,别替它决定。"""
        output, _ = run([{"ref": "pixel:t-1", "judgment": "marketing", "confidence": 0.4}])
        assert output.items[0].route is Route.PENDING


class TestTheFourthLayer:
    """代码复核。**模型说了什么,和它说得对不对,是两件事。**"""

    def test_a_judgment_outside_the_enum_is_refused(self):
        """不猜一个最接近的 —— 那正是错记的来源。"""
        output, _ = run([{"ref": "pixel:t-1", "judgment": "买东西", "confidence": 0.99}])

        (item,) = output.items
        assert item.route is Route.PENDING
        assert item.reason == "check_failed"
        assert output.failed_review == 1

    def test_a_category_outside_the_enum_is_refused(self):
        """自由文本的分类会让报表长出"外卖""点外卖""外卖费"三个类目。"""
        output, _ = run(
            [{"ref": "pixel:t-1", "judgment": "expense", "category": "外卖", "confidence": 0.99}]
        )
        assert output.items[0].route is Route.PENDING
        assert output.items[0].reason == "check_failed"

    def test_an_expense_without_a_category_is_refused(self):
        output, _ = run(
            [{"ref": "pixel:t-1", "judgment": "expense", "category": None, "confidence": 0.99}]
        )
        assert output.items[0].route is Route.PENDING

    def test_amount_must_be_findable_in_the_original_text(self):
        """归一化之后有人改过金额的话,这一条会拦住。"""
        event = an_event(amount="99.00", body="您尾号1234的卡消费人民币38.50元")
        output, _ = run(
            [{"ref": "pixel:t-1", "judgment": "expense", "category": "餐饮", "confidence": 0.99}],
            [event],
        )
        assert output.items[0].route is Route.PENDING
        assert output.items[0].reason == "check_failed"

    def test_the_check_uses_the_text_the_regex_matched(self):
        """**这条是生产里真正走的路。** 交易正文按 R10 脱敏之后 body 是空的,
        只看 body 的话这一层永远返回"通过",变成一个看起来在起作用的空转。"""
        event = an_event(body="", amount="99.00", matched="消费人民币38.50元")
        output, _ = run(
            [{"ref": "pixel:t-1", "judgment": "expense", "category": "餐饮", "confidence": 0.99}],
            [event],
        )
        assert output.items[0].route is Route.PENDING
        assert output.items[0].reason == "check_failed"

    def test_matching_the_regex_snippet_passes(self):
        event = an_event(body="", matched="消费人民币38.50元")
        output, _ = run(
            [{"ref": "pixel:t-1", "judgment": "expense", "category": "餐饮", "confidence": 0.99}],
            [event],
        )
        assert output.items[0].route is Route.DIRECT

    def test_nothing_to_compare_against_is_not_a_failure(self):
        """两个都没有(老数据)时认这条通过:金额本来就来自采集那一步的正则,
        而那一步的输入正是原文。这一层挡的是归一化之后有人改过金额。"""
        event = an_event(body="")
        output, _ = run(
            [{"ref": "pixel:t-1", "judgment": "expense", "category": "餐饮", "confidence": 0.99}],
            [event],
        )
        assert output.items[0].route is Route.DIRECT

    def test_low_confidence_goes_to_the_queue(self):
        output, _ = run(
            [{"ref": "pixel:t-1", "judgment": "expense", "category": "餐饮", "confidence": 0.5}]
        )
        assert output.items[0].route is Route.PENDING
        assert output.items[0].reason == "low_confidence"

    def test_the_threshold_is_configurable(self):
        judged = run(
            [{"ref": "pixel:t-1", "judgment": "expense", "category": "餐饮", "confidence": 0.75}],
            min_confidence=0.7,
        )[0]
        assert judged.items[0].route is Route.DIRECT


class TestGrounding:
    def test_a_reference_to_nothing_is_dropped(self):
        """指不回素材 = 模型编了一条通知。和记忆、日程那边同一条规矩。"""
        output, _ = run([{"ref": "不存在的", "judgment": "expense", "confidence": 0.99}])

        assert output.items == []
        assert output.dropped_ungrounded == 1

    def test_events_without_an_amount_are_not_sent_to_the_model(self):
        """没有金额的事件根本不是交易 —— 不该为它花一次模型调用(铁律 9)。"""
        message = StoredEvent(
            event_id=9,
            event=NormalizedEvent(
                kind=EventKind.MESSAGE,
                title="项目组",
                occurred_at=NOW,
                external_ref=ExternalRef(source="notification", external_id="m-1"),
                trust=Trust.EXTERNAL,
                confidence=1.0,
                body="明天开会",
            ),
        )
        llm = FakeLLM([])
        result = judge(BookkeeperInput(events=[message]), llm=llm)

        assert result.output.considered_events == 0
        assert llm.seen == []  # 一次都没调


class TestWhatGoesToTheModel:
    def test_amount_and_card_are_structured_fields_not_free_text(self):
        """铁律 9:金额、方向作为字段单独给,不混在正文里让模型去抠。"""
        _, llm = run([{"ref": "pixel:t-1", "judgment": "expense", "category": "餐饮",
                       "confidence": 0.9}])

        prompt = json.dumps(llm.seen[0], ensure_ascii=False)
        assert "金额" in prompt and "38.50" in prompt
        # 而且明确告诉模型不要重复它们
        assert "不需要也不要重复" in prompt

    def test_categories_are_listed_in_the_prompt(self):
        _, llm = run([{"ref": "pixel:t-1", "judgment": "expense", "category": "餐饮",
                       "confidence": 0.9}])
        prompt = json.dumps(llm.seen[0], ensure_ascii=False)
        for category in CATEGORIES:
            assert category in prompt


def test_every_judgment_maps_or_refuses_to_map():
    """七个判断里,五个能落账,两个不能 —— 不许有第三种情况。"""
    assert Judgment.EXPENSE.as_txn_kind is TxnKind.EXPENSE
    assert Judgment.MARKETING.as_txn_kind is None
    assert Judgment.PENDING_PAYMENT.as_txn_kind is None
    for judgment in Judgment:
        kind = judgment.as_txn_kind
        assert kind is None or isinstance(kind, TxnKind)


def test_bad_model_output_is_a_failure_not_a_guess():
    from lifein.agents.bookkeeper import BookkeeperFailed

    with pytest.raises(BookkeeperFailed):
        judge(BookkeeperInput(events=[an_event()]), llm=FakeLLM('["不是对象"]'))
