"""交易文本的规则解析(P2 第 2 片)。不需要数据库,也不调模型。

**这一层错了的后果是账本上出现一笔不存在的消费**,而 03 的退出条件写着
"出现错记就停下来补防误判层"。所以这一组的重点不是"能不能解出来",
而是**该解不出来的时候有没有老实返回 None**。

金额那条规则尤其要盯:短信里"尾号1234"和"9月8日"都是数字,
抓错的话账本上会冒出一笔 1234 元。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from lifein.models.normalized import EventKind
from lifein.repos.collector import (
    MATCH_PACKAGE,
    MATCH_SMS_SENDER,
    PURPOSE_MESSAGE,
    PURPOSE_TRANSACTION,
    WhitelistRule,
)
from lifein.repos.transactions import Direction
from lifein.sources import notification
from lifein.sources.notification import DropReason, NotificationAdapter
from lifein.sources.transaction_text import parse

DEVICE = "pixel-7a"


class TestAmount:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("您尾号1234的卡9月8日20:15消费人民币38.50元", Decimal("38.50")),
            ("付款成功 38.50元", Decimal("38.50")),
            ("向星巴克付款 25.00 元", Decimal("25.00")),
            ("工资收入人民币12,500.00元", Decimal("12500.00")),
            ("支出￥9.90", Decimal("9.90")),
            ("消费CNY 1,234.56", Decimal("1234.56")),
        ],
    )
    def test_amounts_that_must_be_found(self, text, expected):
        parsed = parse(None, text)
        assert parsed is not None and parsed.amount == expected

    def test_card_number_is_not_mistaken_for_the_amount(self):
        """**这条是这个模块存在的理由。** 抓错的话账本上会冒出一笔 1234 元。"""
        parsed = parse("招商银行", "您尾号1234的卡9月8日消费人民币38.50元")
        assert parsed.amount == Decimal("38.50")
        assert parsed.account_hint == "1234"

    @pytest.mark.parametrize(
        "text",
        [
            "老王:明天下午三点开会",
            "恭喜您获得0元领取资格,点击查看",  # 营销,金额是 0
            "您的额度已提升至50000",  # 没有"元"也没有货币标记 —— 不是交易
            "",
        ],
    )
    def test_things_that_are_not_transactions(self, text):
        """**宁可漏,不可猜。** 解不出来就返回 None,让它走别的路。"""
        assert parse(None, text) is None


class TestDirectionAndFields:
    @pytest.mark.parametrize(
        ("text", "direction"),
        [
            ("消费人民币38.50元", Direction.DEBIT),
            ("支付宝付款 38.50元", Direction.DEBIT),
            ("信用卡还款5,000.00元", Direction.DEBIT),
            ("工资收入12,500.00元", Direction.CREDIT),
            ("退款9.90元已原路退回", Direction.CREDIT),
            ("转入500.00元", Direction.CREDIT),
        ],
    )
    def test_direction(self, text, direction):
        assert parse(None, text).direction is direction

    def test_unknown_wording_defaults_to_outbound(self):
        """认不出来按流出:多记一笔你会在账本上看见并改掉,漏记不会有人提醒你。"""
        assert parse(None, "尾号1234 交易 88.00元").direction is Direction.DEBIT

    def test_merchant_when_the_wording_gives_one(self):
        assert parse(None, "向星巴克付款 25.00 元").merchant_raw == "星巴克"
        assert parse(None, "商户:全家便利店 消费 12.00元").merchant_raw == "全家便利店"

    def test_no_merchant_is_better_than_a_wrong_one(self):
        """抠不出商户就空着 —— 银行短信里没有商户名,硬编一个会污染归类规则表。"""
        assert parse(None, "您尾号1234的卡消费人民币38.50元").merchant_raw is None

    def test_redacted_fields_keep_only_what_r10_allows(self):
        """R10:交易类只保留金额、时间、卡号后四位、商户,不存整条报文。"""
        parsed = parse("招商银行", "您尾号1234的卡在星巴克消费人民币38.50元,余额12345.67元")
        fields = parsed.redacted_fields()

        assert fields["amount"] == "38.50"
        assert fields["account_hint"] == "1234"
        assert fields["text_redacted"] is True
        # 余额、原文一个字都不该在里面
        assert "12345.67" not in str(fields)
        assert "余额" not in str(fields)


class TestIngestingATransaction:
    """接进采集链路之后的样子(闸门打开时)。"""

    def rules(self, purpose=PURPOSE_TRANSACTION):
        return [
            WhitelistRule(
                id=1,
                match_type=MATCH_SMS_SENDER,
                pattern="95555",
                purpose=purpose,
                enabled=True,
                phase="P2",
            )
        ]

    def screen(self, text, *, purpose=PURPOSE_TRANSACTION, open_transaction=True, monkeypatch=None):
        if open_transaction:
            monkeypatch.setattr(
                notification, "OPEN_PURPOSES", frozenset({PURPOSE_MESSAGE, PURPOSE_TRANSACTION})
            )
        adapter = NotificationAdapter(self.rules(purpose), device_id=DEVICE)
        return adapter.screen(
            {
                "events": [
                    {
                        "channel": "sms",
                        "sender": "955555",
                        "posted_at": "2026-09-08T20:15:00+08:00",
                        "title": "招商银行",
                        "text": text,
                        "external_id": "t-1",
                    }
                ]
            }
        )

    def test_a_bank_sms_becomes_a_transaction_event(self, monkeypatch):
        result = self.screen(
            "您尾号1234的卡9月8日20:15消费人民币38.50元", monkeypatch=monkeypatch
        )

        (event,) = result.events
        assert event.normalized.kind is EventKind.TRANSACTION
        assert event.normalized.amount.value == Decimal("38.50")
        assert event.normalized.amount.direction.value == "debit"
        assert event.occurred_at == datetime.fromisoformat("2026-09-08T20:15:00+08:00")
        # 规则抠出来的那几样跟着进 raw,第 4 层复核要拿 matched 做逐字比对
        assert event.raw["parsed"]["account_hint"] == "1234"
        assert "38.50" in event.raw["parsed"]["matched"]

    def test_marketing_from_a_whitelisted_bank_is_dropped_not_queued(self, monkeypatch):
        """**不进待确认。** 否则队列里会堆满"您有一张优惠券待领取"。"""
        result = self.screen("您有一张优惠券待领取,点击查看", monkeypatch=monkeypatch)

        assert result.events == []
        assert result.dropped == {DropReason.NOT_A_TRANSACTION: 1}

    def test_the_gate_is_open_since_the_twelfth_slice(self):
        """P2 第 12 片之前这条是反的(交易类被 purpose 闸门挡在外面)。
        **现在它是正的,而这个模块一个字都没改** —— 闸门只有一个开关,
        改的是 `notification.OPEN_PURPOSES` 那一行。"""
        adapter = NotificationAdapter(self.rules(), device_id=DEVICE)
        result = adapter.screen(
            {
                "events": [
                    {
                        "channel": "sms",
                        "sender": "955555",
                        "posted_at": "2026-09-08T20:15:00+08:00",
                        "title": "招商银行",
                        "text": "消费人民币38.50元",
                        "external_id": "t-2",
                    }
                ]
            }
        )
        assert result.dropped == {}
        (event,) = result.events
        assert event.normalized.amount.value == Decimal("38.50")

    def test_a_verification_code_from_a_bank_is_still_dropped_first(self, monkeypatch):
        """铁律 11 排在交易解析前面 —— 银行的验证码短信里也有数字。"""
        result = self.screen("您的验证码是 328104,五分钟内有效", monkeypatch=monkeypatch)
        assert result.dropped == {DropReason.VERIFICATION_CODE: 1}


def test_message_purpose_is_untouched():
    """P1 那条链路一个字节都不该变。"""
    rules = [
        WhitelistRule(
            id=2,
            match_type=MATCH_PACKAGE,
            pattern="com.tencent.mm",
            purpose=PURPOSE_MESSAGE,
            enabled=True,
            phase="P1",
        )
    ]
    adapter = NotificationAdapter(rules, device_id=DEVICE)
    result = adapter.screen(
        {
            "events": [
                {
                    "channel": "notification",
                    "source_app": "com.tencent.mm",
                    "posted_at": "2026-09-08T10:00:00+08:00",
                    "title": "项目组",
                    "text": "老王:这顿我付了 38.50 元",
                    "external_id": "m-1",
                }
            ]
        }
    )
    (event,) = result.events
    # 群里提到金额不代表它是一笔交易 —— purpose 决定走哪条路,不是文本内容
    assert event.normalized.kind is EventKind.MESSAGE
    assert event.normalized.amount is None
