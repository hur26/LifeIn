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

**下次再拿到一条真实短信,加进这里。** 这个文件的价值随条数增长,
而每一条的成本是三行。
"""

from __future__ import annotations

from decimal import Decimal

from lifein.repos.transactions import Direction
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
