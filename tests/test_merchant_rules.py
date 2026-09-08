"""商户归类规则表(P2 第 5 片,ADR-008)。

分成两半:**判定商户名**那部分是纯函数,不需要库;**规则的优先级和写回**
全在 SQL 里(ORDER BY、`ON CONFLICT ... DO UPDATE WHERE`),用假仓储测等于
测了个寂寞,所以那一半跑真库。

这一组盯的是 ADR-008 里那句最容易被实现漏掉的话:

> 规则表只从**回填后的真实商户名**沉淀,不要用代收机构名污染规则表。

漏掉它的后果不是报错,是表里出现一条 `财付通 → 餐饮`,
然后你在便利店、加油站、药店的每一笔都被归成餐饮 —— 而 `hit_count`
会一直涨,让这条错规则看起来**越用越像是对的**。
"""

from __future__ import annotations

import pytest

from lifein.repos import merchant_rules
from lifein.repos.merchant_rules import CreatedBy, MatchType, normalize

OTHER_USER = "99999999-9999-9999-9999-999999999999"


class TestMerchantNames:
    """纯函数那一半。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("星巴克", "星巴克"),
            (" 星巴克 ", "星巴克"),
            ("星巴克 咖啡", "星巴克咖啡"),
            ("-全家便利店-", "全家便利店"),
            ("商户:麦当劳", "商户:麦当劳"),  # 中间的冒号不动,只削首尾
            ("", None),
            (None, None),
            ("   ", None),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize(raw) == expected

    @pytest.mark.parametrize(
        "name",
        ["财付通", "财付通-某某商户", "支付宝(中国)网络技术有限公司", "微信支付", "银联商务"],
    )
    def test_intermediaries_are_recognized_by_containment(self, name):
        """**用包含判定。** 银行短信里它们从来不是干干净净单独出现的。"""
        assert merchant_rules.is_intermediary(name)

    @pytest.mark.parametrize("name", ["星巴克", "全家便利店", "中国石化"])
    def test_real_merchants_are_not(self, name):
        assert not merchant_rules.is_intermediary(name)

    def test_an_empty_name_counts_as_intermediary(self):
        """空的当然不能进规则表 —— 让判定函数把这种情况一起吃掉,
        免得每个调用点各写一次 None 检查,再各漏一次。"""
        assert merchant_rules.is_intermediary(None)


class TestTheMonitoringMetric:
    def test_llm_share(self):
        assert merchant_rules.llm_share({"by_rule": 80, "by_llm": 20}) == 0.2
        assert merchant_rules.llm_share({"by_rule": 0, "by_llm": 5}) == 1.0

    def test_no_data_is_none_not_zero(self):
        """一条都没归过的时候占比是"不知道",不是 0 ——
        报成 0 会让"沉淀得真好"和"根本没跑"长得一模一样。"""
        assert merchant_rules.llm_share({}) is None


@pytest.mark.integration
class TestRules:
    """需要真实 PostgreSQL:优先级排序和写回保护都写在 SQL 里。"""

    def test_a_remembered_rule_is_found_again(self, pg_session, user_id):
        merchant_rules.remember(user_id, pg_session, merchant="星巴克", category="餐饮")

        result = merchant_rules.categorize(user_id, pg_session, merchant="星巴克")
        assert result.category == "餐饮"
        assert result.by_rule is True

    def test_a_rule_beats_the_model(self, pg_session, user_id):
        """规则命中时**不采用模型这一次的答案** —— 沉淀下来的比一次推断可信。"""
        merchant_rules.remember(user_id, pg_session, merchant="星巴克", category="餐饮")

        result = merchant_rules.categorize(
            user_id, pg_session, merchant="星巴克", llm_category="购物"
        )
        assert result.category == "餐饮"

    def test_a_miss_falls_back_to_the_model(self, pg_session, user_id):
        result = merchant_rules.categorize(
            user_id, pg_session, merchant="没见过的店", llm_category="购物"
        )
        assert (result.category, result.by_rule) == ("购物", False)

    def test_a_miss_without_a_model_answer_stays_uncategorized(self, pg_session, user_id):
        """归不出来就空着。**不许兜底成"其他"** ——
        那样报表上的"其他"会同时装着"真的是其他"和"没归出来",两者再也分不开。"""
        result = merchant_rules.categorize(user_id, pg_session, merchant="没见过的店")
        assert result.category is None

    def test_hits_are_counted(self, pg_session, user_id):
        """`hit_count` 是判断规则有没有用的唯一依据 —— 不涨就说明它白建了。"""
        merchant_rules.remember(user_id, pg_session, merchant="星巴克", category="餐饮")
        for _ in range(3):
            merchant_rules.categorize(user_id, pg_session, merchant="星巴克")

        (rule,) = merchant_rules.list_rules(user_id, pg_session)
        assert rule.hit_count == 3

    class TestWhatMustNotBeRemembered:
        """**ADR-008 那句话。**"""

        @pytest.mark.parametrize("name", ["财付通", "财付通-某某商户", "支付宝"])
        def test_intermediaries_never_become_rules(self, pg_session, user_id, name):
            assert merchant_rules.remember(
                user_id, pg_session, merchant=name, category="餐饮"
            ) is None
            assert merchant_rules.list_rules(user_id, pg_session) == []

        def test_a_category_outside_the_enum_is_refused(self, pg_session, user_id):
            """和记账 agent 第 4 层同一个枚举。两处都挡,是因为写回这条路
            不一定经过那个 agent —— 用户手改也走 remember()。"""
            assert merchant_rules.remember(
                user_id, pg_session, merchant="星巴克", category="外卖"
            ) is None

        def test_a_one_character_name_is_refused(self, pg_session, user_id):
            """一个字做前缀会匹配上一大片东西。"""
            assert merchant_rules.remember(
                user_id, pg_session, merchant="店", category="餐饮"
            ) is None

        def test_rejection_is_silent(self, pg_session, user_id):
            """返回 None 而不是抛异常:实时那一遍本来就大多抠不出真商户,
            这是常态。抛异常的话记账链路会被自己的正常情况打断。"""
            merchant_rules.remember(user_id, pg_session, merchant="财付通", category="餐饮")
            # 没炸,后面照跑
            result = merchant_rules.categorize(user_id, pg_session, merchant="财付通")
            assert result.category is None

    class TestPriority:
        def test_exact_beats_prefix(self, pg_session, user_id):
            merchant_rules.remember(
                user_id, pg_session, merchant="星巴克", category="购物",
                match_type=MatchType.PREFIX,
            )
            merchant_rules.remember(
                user_id, pg_session, merchant="星巴克咖啡", category="餐饮",
            )

            assert merchant_rules.categorize(
                user_id, pg_session, merchant="星巴克咖啡"
            ).category == "餐饮"

        def test_the_user_beats_the_model(self, pg_session, user_id):
            """**用户改过一次的东西,不该被下个月的模型再改回去。**"""
            merchant_rules.remember(
                user_id, pg_session, merchant="全家便利店", category="购物",
                created_by=CreatedBy.USER,
            )
            merchant_rules.remember(
                user_id, pg_session, merchant="全家便利店", category="餐饮",
                created_by=CreatedBy.LLM,
            )

            assert merchant_rules.categorize(
                user_id, pg_session, merchant="全家便利店"
            ).category == "购物"

        def test_the_user_can_still_correct_a_model_rule(self, pg_session, user_id):
            """反过来要走得通,否则"改不动"和"保护住了"是同一个症状。"""
            merchant_rules.remember(user_id, pg_session, merchant="全家便利店", category="餐饮")
            merchant_rules.remember(
                user_id, pg_session, merchant="全家便利店", category="购物",
                created_by=CreatedBy.USER,
            )

            assert merchant_rules.categorize(
                user_id, pg_session, merchant="全家便利店"
            ).category == "购物"

        def test_a_longer_prefix_wins(self, pg_session, user_id):
            for pattern, category in (("中国石", "交通"), ("中国石化加油", "交通")):
                merchant_rules.remember(
                    user_id, pg_session, merchant=pattern, category=category,
                    match_type=MatchType.PREFIX,
                )
            rule = merchant_rules.lookup(user_id, pg_session, merchant="中国石化加油站朝阳店")
            assert rule.pattern == "中国石化加油"

    def test_rules_do_not_leak_between_users(self, pg_session, user_id):
        """铁律 1。归类规则里带着一个人去过哪些店。"""
        merchant_rules.remember(user_id, pg_session, merchant="星巴克", category="餐饮")

        assert merchant_rules.categorize(
            OTHER_USER, pg_session, merchant="星巴克"
        ).category is None
        assert merchant_rules.list_rules(OTHER_USER, pg_session) == []

    def test_a_wrong_rule_can_be_deleted(self, pg_session, user_id):
        """归错类的规则要能删掉,否则只能靠新规则去盖。"""
        rule = merchant_rules.remember(user_id, pg_session, merchant="星巴克", category="购物")

        assert merchant_rules.forget(user_id, pg_session, rule_id=rule.id) is True
        assert merchant_rules.list_rules(user_id, pg_session) == []

    def test_deleting_someone_elses_rule_does_nothing(self, pg_session, user_id):
        rule = merchant_rules.remember(user_id, pg_session, merchant="星巴克", category="餐饮")

        assert merchant_rules.forget(OTHER_USER, pg_session, rule_id=rule.id) is False
        assert len(merchant_rules.list_rules(user_id, pg_session)) == 1
