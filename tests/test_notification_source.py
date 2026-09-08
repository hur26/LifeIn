"""采集上报的筛选链 —— 白名单、purpose 闸门、验证码、归一化。

不需要数据库:白名单规则是传进去的,所以这条链能逐条验。
**它也确实该逐条验** —— 这里漏一条的后果是别人的私聊或你的验证码进了库
(R10),而那种漏法在运行时没有任何外部表现。
"""

from __future__ import annotations

from datetime import datetime

from lifein.models.normalized import EventKind, Flag, Trust
from lifein.repos.collector import MATCH_PACKAGE, MATCH_SMS_SENDER, WhitelistRule
from lifein.sources import notification
from lifein.sources.notification import DropReason, NotificationAdapter
from lifein.sources.verification_code import looks_like_verification_code

DEVICE = "pixel-7a"
WECHAT = "com.tencent.mm"


def rule(
    pattern: str = WECHAT,
    *,
    match_type: str = MATCH_PACKAGE,
    purpose: str = "message",
    enabled: bool = True,
) -> WhitelistRule:
    return WhitelistRule(
        id=1,
        match_type=match_type,
        pattern=pattern,
        purpose=purpose,
        enabled=enabled,
        phase="P1",
    )


def item(**overrides) -> dict:
    base = {
        "channel": "notification",
        "source_app": WECHAT,
        "posted_at": "2026-09-08T10:11:12+08:00",
        "title": "项目组",
        "text": "老王:明天下午三点开会",
        "external_id": "n-1",
    }
    return {**base, **overrides}


def screen(items: list[dict], rules: list[WhitelistRule] | None = None):
    adapter = NotificationAdapter(
        rules if rules is not None else [rule()], device_id=DEVICE
    )
    return adapter.screen({"device_id": DEVICE, "events": items})


def test_whitelisted_notification_becomes_an_external_message():
    result = screen([item()])

    assert result.dropped == {}
    (event,) = result.events
    assert event.source == "notification"
    # 去重键拼了设备:两台手机可能各自生成同一个通知 id
    assert event.external_id == f"{DEVICE}:n-1"
    assert event.trust is Trust.EXTERNAL
    assert event.normalized.kind is EventKind.MESSAGE
    assert event.normalized.title == "项目组"
    assert event.normalized.body == "老王:明天下午三点开会"
    assert event.normalized.occurred_at == datetime.fromisoformat("2026-09-08T10:11:12+08:00")
    # 原文留在 raw:解析器改好之后要能按同一个键重跑
    assert event.raw["text"] == "老王:明天下午三点开会"


def test_default_is_deny():
    """不在白名单里的一律不入库 —— 手机端过滤过一次不代表这里可以省。"""
    result = screen([item(source_app="com.some.other.app")])

    assert result.events == []
    assert result.dropped == {DropReason.NOT_WHITELISTED: 1}


def test_package_match_is_exact_not_prefix():
    """前缀匹配会让 com.tencent.mm 顺带放行 com.tencent.mm.fake。"""
    result = screen([item(source_app="com.tencent.mm.fake")])
    assert result.dropped == {DropReason.NOT_WHITELISTED: 1}


def test_disabled_rule_stops_letting_things_through():
    result = screen([item()], rules=[rule(enabled=False)])
    assert result.dropped == {DropReason.NOT_WHITELISTED: 1}


def test_the_transaction_gate_is_open_since_p2():
    """P2 第 12 片打开的那一行。**它排在那一期倒数第三片是刻意的** ——
    闸门一开真钱的数据就开始流进来,那之后再出的错发生在真实账本上,
    所以四层防误判、两阶段入账、覆盖率巡检全部先建好了才动它。"""
    result = screen(
        [item(source_app="com.eg.android.AlipayGphone", text="消费人民币38.50元")],
        rules=[rule("com.eg.android.AlipayGphone", purpose="transaction")],
    )

    assert result.dropped == {}
    (event,) = result.events
    assert event.normalized.kind is EventKind.TRANSACTION


def test_closing_the_gate_again_is_safe(monkeypatch):
    """03 的退出条件写着"出现错记 → 停下来补防误判层",而"停下来"的动作
    就是把 `PURPOSE_TRANSACTION` 从 `OPEN_PURPOSES` 里去掉。

    **去掉之后交易类退回 phase_not_open,已经入账的一笔都不动** ——
    这条钉住的是"关得掉",而不是"关了会怎样"。
    """
    monkeypatch.setattr(notification, "OPEN_PURPOSES", frozenset({"message"}))
    result = screen(
        [item(source_app="com.eg.android.AlipayGphone", text="消费人民币38.50元")],
        rules=[rule("com.eg.android.AlipayGphone", purpose="transaction")],
    )

    assert result.events == []
    assert result.dropped == {DropReason.PHASE_NOT_OPEN: 1}


def test_verification_code_is_dropped_even_from_a_whitelisted_source():
    """铁律 11 的服务端那一道:来源合法也照丢。"""
    result = screen([item(title="微信", text="您的验证码是 328104,五分钟内有效")])

    assert result.events == []
    assert result.dropped == {DropReason.VERIFICATION_CODE: 1}


def test_verification_code_in_the_title_alone_counts():
    result = screen([item(title="【某银行】动态密码", text="点击查看")])
    assert result.dropped == {DropReason.VERIFICATION_CODE: 1}


def test_sms_sender_matches_by_prefix():
    """银行短信的号码是号段,写死全等等于每换一个下发通道就漏一批。"""
    result = screen(
        [item(channel="sms", source_app=None, sender="1069001234567", text="会议改到周四")],
        rules=[rule("10690", match_type=MATCH_SMS_SENDER)],
    )

    (event,) = result.events
    assert event.raw["channel"] == "sms"
    party = event.normalized.parties[0]
    assert party.identifier == "1069001234567"


def test_aggregated_notifications_are_flagged():
    """系统折叠掉的那些拿不回来,下游得知道自己看的是残缺的。"""
    result = screen([item(text="[3 条] 张三: 明天见")])
    (event,) = result.events
    assert Flag.AGGREGATED in event.normalized.flags


def test_items_without_a_dedup_key_or_time_are_refused():
    """没有 external_id 就没有去重键,重发一次就多一条。"""
    result = screen([item(external_id=""), item(posted_at=None), item(posted_at="2026-09-08")])

    assert result.events == []
    # 最后那条是"无时区",和没有时间一样不能收 —— 收了会悄悄错八小时
    assert result.dropped == {DropReason.MALFORMED: 3}


def test_one_bad_item_does_not_take_down_the_batch():
    result = screen([item(), item(external_id=""), item(external_id="n-2")])

    assert [e.external_id for e in result.events] == [f"{DEVICE}:n-1", f"{DEVICE}:n-2"]
    assert result.dropped == {DropReason.MALFORMED: 1}


class TestVerificationCodePattern:
    """正则本身。07 §4 说它只能放宽不能收窄,所以这些用例只会变多。"""

    def test_common_wordings(self):
        assert looks_like_verification_code("您的验证码是 123456")
        assert looks_like_verification_code("校验码 8823,请勿告诉他人")
        assert looks_like_verification_code("动态密码:4432")
        assert looks_like_verification_code("Your verification code is 993022")

    def test_ordinary_messages_pass(self):
        assert not looks_like_verification_code("明天下午三点开会")
        assert not looks_like_verification_code("你的快递已签收")

    def test_parts_are_matched_separately(self):
        # 标题和正文分开看:拼起来才像命中的那种不该算
        assert not looks_like_verification_code("今天很动态", "密码保护已开启")
        assert looks_like_verification_code("微信", "验证码")

    def test_none_and_empty_are_safe(self):
        assert not looks_like_verification_code(None, "")


class TestTheP2Presets:
    """P2 建议放行的那些来源。**它们只是建议,加了才生效** —— 默认拒绝不变。"""

    def test_bank_numbers_are_prefixes(self):
        """**95555 是主号,银行实际发短信用的是 955550、9555501……**
        全等匹配会漏掉绝大多数,而这种漏是静默的。"""
        from lifein.repos.collector import MATCH_SMS_SENDER
        from lifein.sources import bank_sources

        assert all(item.match_type == MATCH_SMS_SENDER for item in bank_sources.BANK_SMS)
        result = screen(
            [item(sender="9555501", text="消费人民币38.50元", source_app=None)],
            rules=[rule("95555", match_type=MATCH_SMS_SENDER, purpose="transaction")],
        )
        assert len(result.events) == 1

    def test_package_names_are_exact(self):
        """包名是精确的。前缀会误伤 —— `com.icbc` 会连上 `com.icbcxxx`,
        而那可能是别人的应用。"""
        from lifein.repos.collector import MATCH_PACKAGE
        from lifein.sources import bank_sources

        assert all(item.match_type == MATCH_PACKAGE for item in bank_sources.PAYMENT_APPS)

    def test_every_preset_is_a_transaction_source(self):
        from lifein.repos.collector import PURPOSE_TRANSACTION
        from lifein.sources import bank_sources

        assert all(item.purpose == PURPOSE_TRANSACTION for item in bank_sources.ALL)
        assert all(item.phase == "P2" for item in bank_sources.ALL)

    def test_presets_are_findable_by_name_or_number(self):
        from lifein.sources import bank_sources

        assert [i.pattern for i in bank_sources.by_label("招商")] == ["95555", "cmb.pb"]
        assert [i.label for i in bank_sources.by_label("95533")] == ["建设银行"]
        assert bank_sources.by_label("没有这家") == []
