"""三条**真实短信**过一遍规则层。不需要数据库,也不调模型。

来源是 2026-09 白杨手机上的三条,原文一字未改(只把姓名留着 —— 那条正是要
验证"人名不会被当成商户"的)。**它们不是构造出来的用例**,而这一点是重点:
前面那些用例是照着规则写的,所以规则漏掉什么,用例也会漏掉什么。

**这三条第一次跑的时候,前两条各暴露一个真问题:**

| 短信 | 当时的结果 | 问题 |
| --- | --- | --- |
| 招行消费 70 元 | 卡号 `None` | 短信写的是"储蓄卡0361",一个"尾号"都没有 |
| 南方基金定投预告 | 解出 `50 元 / debit` | **这不是已发生的交易,是预告** |
| 阿里验证码 | 丢弃 | 对的 |

第二条是会真记错账的那一类:预告被记一笔,**下个月真扣款时再记一笔**。
而四层防误判里第 1、2、4 层都不拦它 —— 第 4 层查的是"金额能不能逐字找到"、
"分类在不在枚举内"、"置信度够不够",这三条它全过。

**2026-09-11 又来了三条,而这三条推翻的是一个设计,不是一个 bug**
(ADR-034)。它们证明"按发件号码匹配银行"这条路根本走不通 ——
细节在下面 `TestTheSenderIsNotAnIdentity` 里。

**下次再拿到一条真实短信,加进这里。** 这个文件的价值随条数增长,
而每一条的成本是三行。
"""

from __future__ import annotations

from decimal import Decimal

from lifein.repos.transactions import Direction
from lifein.sources.sms_signature import signature_of
from lifein.sources.transaction_text import looks_unpaid_yet, parse, redact_for_model
from lifein.sources.verification_code import looks_like_verification_code

CMB_SPEND = (
    "【招商银行】",
    "您的招商银行储蓄卡0361于09月09日09:40，"
    "在南方基金-南方现金通货币E一网通支付人民币70.00元。",
)

FUND_PLAN = (
    "【南方基金】",
    "尊敬的白杨，您好！您通过南方基金电子直销平台设定的南方纳斯达克100指数"
    "发起（QDII）I(021000)定投计划将于近期扣款，扣款时间2026年09月10日，"
    "预计扣款金额50元。请保证定投扣款日前银行卡内有足够金额，"
    "如定投金额超过产品限额则按产品限额发起扣款，"
    "详情请登录南方基金APP、微信服务号或官网查询。祝您投资理财愉快！。"
    "退订短信回复QXDT",
)

CMB_SALARY = (
    "【招商银行】",
    "您账户0361于09月10日16:34入账工资，人民币4321.00。",
)
"""2026-09-11 从一批历史短信里捞出来的形状。**一条进账被记成了支出。**

**只有金额是改过的**(原文是一笔工资,数额不该进版本库),其余一字未动 ——
而那三个 bug 和数额无关,只和"四位以上"有关。
"""

ALI_CODE = (
    "【阿里巴巴】",
    "验证码:932525，操作：您正在短信登录，5分钟内有效。"
    "   Verification code:932525, valid for 5 minutes.",
)


class TestARealPurchase:
    """招行的消费短信。这一条**应该**被记账。"""

    def test_it_parses(self):
        parsed = parse(*CMB_SPEND)

        assert parsed is not None
        assert parsed.amount == Decimal("70.00")
        assert parsed.direction is Direction.DEBIT

    def test_the_card_tail_is_found(self):
        """**这一条第一次跑的时候是红的。**

        `_ACCOUNT_HINT` 原来只认"尾号|卡号|账号|末四位|尾数",而招行写的是
        "储蓄卡0361"。抠不到的后果不显眼但很实在:跨渠道合并和对账匹配
        都少了一维,而那正是"两张卡里各有一笔同额"时唯一能区分它们的东西。
        """
        assert parse(*CMB_SPEND).account_hint == "0361"

    def test_the_merchant_is_the_fund_not_the_bank(self):
        """商户是"在……"后面那一段,不是发短信的银行。

        归类靠它:记成"招商银行"的话,`merchant_rules` 会学到一条
        "招商银行 → 某某分类",而下一笔在别处的消费也会被归成那一类。
        """
        assert parse(*CMB_SPEND).merchant_raw == "南方基金-南方现金通货币E一网通"

    def test_the_date_is_not_mistaken_for_the_amount(self):
        """`0361`、`09月09日`、`09:40` 全是数字,而金额是 `70.00`。

        金额规则要求带货币标记或"元" —— 这一条就是那条规则存在的理由。
        """
        assert parse(*CMB_SPEND).matched_amount_text == "人民币70.00"

    def test_the_card_number_does_not_reach_the_model(self):
        """卡号已经作为结构化字段抠出来了,而**模型判断类型不需要它**。

        第一次跑的时候这里也是漏的:遮罩用的是另一份关键词清单,
        同样不认识"储蓄卡" —— 于是那个卡号既被抠出来了**又原样发了出去**。
        """
        out = redact_for_model(CMB_SPEND[1])

        assert "0361" not in out
        assert "储蓄卡****" in out
        # 商户和金额还在 —— 模型判断类型全靠它们
        assert "南方基金-南方现金通货币E一网通" in out
        assert "70.00" in out


class TestAPaymentThatHasNotHappened:
    """基金定投预告。**这一条绝不该被记账。**

    它是"账本上凭空多一笔"里最容易发生的那一种,而这类短信在真实生活里
    非常常见:基金定投、信用卡账单、水电缴费。
    """

    def test_it_is_not_a_transaction(self):
        """**这一条第一次跑的时候是红的**:规则层解出了 50 元 / debit,
        于是它会变成一条 `kind=transaction` 的事件送进记账链路,
        而那之后只剩模型那一层能拦。"""
        assert parse(*FUND_PLAN) is None

    def test_the_signal_is_the_future_wording(self):
        """判据是**动词跟着未来的标记**,而这条里有三处:
        "将于近期扣款"、"预计扣款金额"、"请保证……有足够金额"。"""
        assert looks_unpaid_yet(FUND_PLAN[1]) is True

    def test_a_real_purchase_is_not_dropped_by_it(self):
        """**反过来不能误伤。** 这条规则拦的是"钱还没动",
        不是"文本里出现了预计两个字"。"""
        assert looks_unpaid_yet(CMB_SPEND[1]) is False
        assert looks_unpaid_yet("消费38.50元，预计3天后出账单") is False
        assert looks_unpaid_yet("信用卡还款5,000.00元已入账") is False

    def test_the_next_month_would_have_been_a_second_entry(self):
        """把这两条摆在一起看,才看得出为什么它必须在规则层挡掉:
        预告和真扣款的金额一样、方向一样、商户都指向同一家基金 ——
        **两条都记下来的话,那是同一笔钱在账本上出现了两次**。
        """
        real = parse("【招商银行】", "您的储蓄卡0361于09月10日扣款人民币50.00元。")

        assert parse(*FUND_PLAN) is None
        assert real is not None and real.amount == Decimal("50.00")


class TestAVerificationCode:
    """铁律 11:验证码一个字都不许留。"""

    def test_it_is_dropped_before_anything_else(self):
        assert looks_like_verification_code(*ALI_CODE) is True

    def test_it_never_reaches_the_transaction_parser(self):
        """**闸门排在解析前面。** 排在后面的话,`932525` 会被某条规则
        当成别的东西 —— 而验证码短信里全是数字。"""
        assert looks_like_verification_code(*ALI_CODE) is True


# ---------- 2026-09-11 的三条:同一台手机,同一个短信应用 ----------
#
# 只记通知上显示的发件人与正文开头的签名 —— 正文本身不再抄一遍,
# 这三条要验的是"发件人这个字段到底是什么",和内容无关。

REAL_SENDERS = [
    # (通知标题里的发件人, 正文开头的签名, 这条其实是谁发的)
    ("招商银行", "【招商银行】您的招商银行储蓄卡0361于09月11日10:22...", "招商银行"),
    ("游戏中心", "【游戏中心】...", "游戏中心"),
    ("10693495555", "【招商银行】您的招商银行储蓄卡0361于09月11日10:22...", "招商银行"),
]


class TestTheSenderIsNotAnIdentity:
    """**"发件号码"这个东西在通知监听这条路上不存在**(ADR-034)。

    App 没有 `READ_SMS`,看到的是系统渲染给人看的标题。同一个
    `com.android.mms`,同一台手机,三条短信给出了两种完全不同的东西。
    """

    def test_the_notification_title_is_sometimes_a_name_and_sometimes_a_number(self):
        shown = [sender for sender, _body, _who in REAL_SENDERS]
        assert [value.isdigit() for value in shown] == [False, False, True]

    def test_the_real_number_does_not_start_with_the_bank_number(self):
        """**这一条是那 14 条号段预设失败的真正原因,和显示名无关。**

        招行走 1069 的 SP 网关下发,`95555` 在里面是**子串不是前缀** ——
        也就是说就算每次都拿得到号码,前缀这个假设本身也是错的。
        """
        real = "10693495555"
        assert not real.startswith("95555")
        assert "95555" in real

    def test_the_signature_is_stable_across_both_shapes(self):
        """签名由发信方写进内容,三条里三条都指向正确的机构 ——
        **包括标题是网关号码那条**。"""
        for _sender, body, who in REAL_SENDERS:
            assert signature_of(body) == who

    def test_a_signature_rule_matches_regardless_of_what_the_title_showed(self):
        from lifein.repos.collector import MATCH_SMS_SIGNATURE, WhitelistRule

        cmb = WhitelistRule(
            id=1,
            match_type=MATCH_SMS_SIGNATURE,
            pattern="招商银行",
            purpose="transaction",
            enabled=True,
            phase="P2",
        )
        hits = [
            cmb.matches(package_name="com.android.mms", sender=sender, signature=signature_of(body))
            for sender, body, _who in REAL_SENDERS
        ]
        # 招行那两条都命中,游戏中心那条不命中 —— 而它们的标题长得毫无规律
        assert hits == [True, False, True]


class TestAnIncomingSalary:
    """**这一条同时打出三个洞**,而它们全都活过了 1349 条用例。

    共同原因是那些用例都是照着规则写的:金额挑的是 70.00、38.50、10.00,
    没有一笔上四位;方向测的是消费,没测过进账;卡号写的是"储蓄卡",
    没写过"账户"。规则漏掉什么,用例也就漏掉什么。
    """

    def test_four_digit_amounts_are_not_truncated(self):
        r"""**`人民币5000.00` 曾经解出 500,`人民币12345.67` 解出 123。**

        千分位那一支写的是 `\d{1,3}(?:,\d{3})*`,`*` 不要求真的有逗号,
        于是它吃掉前三位就收工。带 `元` 后缀的那一支因为要回溯匹配 `元`
        侥幸躲过 —— 而货币前缀那一支后面没有锚。
        """
        got = parse(*CMB_SALARY)
        assert got is not None
        assert got.amount == Decimal("4321.00")

    def test_a_thousands_separator_still_works(self):
        """改成 `+` 之后真带逗号的那种不能跟着坏掉。"""
        got = parse(None, "【招商银行】消费人民币1,234.56元")
        assert got is not None
        assert got.amount == Decimal("1234.56")

    def test_money_coming_in_is_not_an_expense(self):
        """`入账` 原来两个词表都不在,于是落到"认不出来按流出"那条默认。

        那条默认是给"真认不出来"的,不是给"词表漏了一个常用词"的。
        """
        got = parse(*CMB_SALARY)
        assert got is not None
        assert got.direction is Direction.CREDIT

    def test_the_card_number_is_found_when_the_bank_says_account(self):
        got = parse(*CMB_SALARY)
        assert got is not None
        assert got.account_hint == "0361"

    def test_that_card_number_never_reaches_the_model(self):
        """**这是三个里最要紧的一个。**

        `_MASK_ACCOUNT` 和 `account_hint` 用的是同一份词表,所以词表漏了
        "账户",那个卡号既抠不出来、也**不会被打码** —— 它原样进了送给
        外部模型的那一份(R12)。一个词表两处用是刻意的:补一个词,
        抽取和脱敏同时跟上,不会只修一半。
        """
        redacted = redact_for_model(CMB_SALARY[1])
        assert "0361" not in redacted
        assert "****" in redacted
        # 金额要留着 —— 模型判断类型需要它
        assert "4321.00" in redacted
