"""送进 prompt 的交易正文要遮掉卡号和余额(06 §5 第 5 条)。不需要数据库。

铁律 9 那条"能用规则拿到的字段不进正文"**原来只做了一半**:结构化字段抽出来了,
正文照样原样送出去 —— 于是一条

    您尾号1234的储蓄卡9月8日消费人民币38.50元,账户余额12,345.67元。

里的卡号和余额一起去了外部供应商那里,尽管记账 agent 这一轮只回答
"这是哪一类资金变动",两样都用不上。余额尤其糟:它甚至不在我们要存的清单里
([R10](../docs/05-risks.md#r10--手机端采集器的越权读取))。

**这一组分两半,两半同样重要:**

- 遮住了不该出去的(卡号、余额、账号)
- **没遮住该留下的**(金额、商户)—— 遮过头的表现是模型看到"支出****元",
  然后把每一条都判成看不懂,而那会让整个队列淹掉
"""

from __future__ import annotations

import pytest

from lifein.agents.bookkeeper import build_blocks
from lifein.sources.transaction_text import redact_for_model


class TestWhatMustNotGoOut:
    def test_the_balance_is_masked(self):
        """**余额是这条短信里最敏感的一个数字**,而它甚至不在要存的清单里。"""
        out = redact_for_model("消费38.50元,账户余额12,345.67元")
        assert "12,345.67" not in out
        assert "余额****" in out

    def test_the_credit_limit_too(self):
        """可用额度和余额一样是账户状态,不是这笔交易。"""
        assert "9,000.00" not in redact_for_model("消费38.50元,可用额度 9,000.00元")

    def test_the_card_tail_is_masked(self):
        """卡号已经作为 `account_hint` 抠出来了,而**模型判断类型不需要它** ——
        给模型的结构化字段里本来就没有它。"""
        out = redact_for_model("您尾号1234的储蓄卡消费38.50元")
        assert "1234" not in out
        assert "尾号****" in out

    def test_a_full_card_number_is_masked(self):
        assert "6222021234567890" not in redact_for_model(
            "您的信用卡(6222021234567890)消费38.50元"
        )

    def test_a_phone_number_is_masked(self):
        assert "13800138000" not in redact_for_model("转账给13800138000,金额50.00元")


class TestWhatMustSurvive:
    """**遮过头和没遮一样糟。**

    金额是这条通知的主语,遮掉之后模型判不出"消费"和"待支付提醒"的区别 ——
    而那正是四层防误判要它做的事。
    """

    @pytest.mark.parametrize(
        ("text", "amount"),
        [
            ("消费人民币38.50元", "38.50"),
            ("向星巴克付款38.5元", "38.5"),
            ("支出人民币1234.56元,余额8888.88元", "1234.56"),
            ("已支付 128.00 元", "128.00"),
            # 12,345.67 里每一段都短于六位,而长数字那条规则不许误伤它
            ("消费人民币12,345.67元", "12,345.67"),
            # **这两条盯的是回退。** `\d{6,}` 会退回六位去凑一个匹配,
            # 于是 1234567.89 被遮成 ****7.89 —— 比不遮更糟:
            # 金额看起来还在,只是变成了另一个数
            ("消费1234567.89元", "1234567.89"),
            ("消费1234567元", "1234567"),
        ],
    )
    def test_the_amount_survives(self, text, amount):
        assert amount in redact_for_model(text)

    def test_the_merchant_survives(self):
        """商户名是模型判断类型的主要依据,遮掉它这一层就废了。"""
        assert "星巴克" in redact_for_model("您向星巴克付款38.5元")

    def test_the_verbs_survive(self):
        """"消费""退款""还款"这几个词决定 kind。它们不是数字,但值得钉一下 ——
        下一次有人加规则时,这条会挡住"顺手把整句话遮掉"。"""
        out = redact_for_model("您尾号1234的卡还款5,000.00元,余额1元")
        assert "还款" in out and "5,000.00" in out

    def test_nothing_in_nothing_out(self):
        assert redact_for_model(None) == ""
        assert redact_for_model("") == ""


class TestItIsActuallyApplied:
    """**遮罩写好了不等于用上了。**

    这一条盯的是 `build_blocks` —— 那是正文真正离开这个系统的地方。
    只测 `redact_for_model` 的话,某天有人在别处直接拼 `event.body`,
    这一组照样是绿的。
    """

    def test_the_block_text_is_redacted(self):
        from datetime import UTC, datetime
        from decimal import Decimal

        from lifein.models.normalized import (
            Amount,
            Direction,
            EventKind,
            ExternalRef,
            NormalizedEvent,
            Trust,
        )
        from lifein.repos.raw_events import StoredEvent

        event = NormalizedEvent(
            kind=EventKind.TRANSACTION,
            title="招商银行",
            occurred_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
            external_ref=ExternalRef(source="notification", external_id="t-1"),
            trust=Trust.EXTERNAL,
            confidence=1.0,
            amount=Amount(value=Decimal("38.50"), currency="CNY", direction=Direction.DEBIT),
            body="您尾号1234的储蓄卡消费人民币38.50元,账户余额12,345.67元。",
        )
        (block,) = build_blocks([StoredEvent(event_id=1, event=event, raw=None)])

        assert "1234" not in block.text
        assert "12,345.67" not in block.text
        # 金额还在,而且结构化字段那一份也还在
        assert "38.50" in block.text
        assert block.fields["金额"].startswith("38.50")
        # **卡号一个字段都没有** —— 判断类型用不上它
        assert not any("卡" in name or "尾号" in name for name in block.fields)
