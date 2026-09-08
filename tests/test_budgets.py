"""预算与超支预警(P2 第 9 片)。需要真实 PostgreSQL:算的全是聚合查询。

03 给这一片的验收标准只有一句,而它决定了整片的形状:

> 预算超支预警在**超支当天**发出,不是月底

所以这一组盯的是**时机**和**只说一次**,不是数字算得对不对(那部分是 SQL
的事,一条 sum 而已)。具体是三件:

1. 超支那一刻的下一次扫描就说话 —— 而不是等到月底
2. 90% 和 100% 各说一次,**不能共用一个冷却期** ——
   共用的话,真正超支那天会被 90% 那天的冷却挡掉,而那天恰恰最该说话
3. 一条预算超了之后每天都还是超着,所以一个周期只说一次;下个周期重新开始
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from lifein.repos import budgets, transactions
from lifein.repos.budgets import BudgetError
from lifein.repos.transactions import Direction, TxnKind
from lifein.rules.base import RuleContext
from lifein.rules.budget import BUDGET_ALERT

pytestmark = pytest.mark.integration

SHANGHAI = timezone(timedelta(hours=8))
MID_AUGUST = datetime(2026, 8, 15, 12, 0, tzinfo=SHANGHAI)


def a_spend(
    session,
    user_id,
    *,
    amount: str,
    category: str | None = "餐饮",
    kind: TxnKind = TxnKind.EXPENSE,
    when: datetime = MID_AUGUST,
    external_id: str | None = None,
):
    event_id = session.execute(
        text(
            "INSERT INTO raw_events (user_id, source, external_id, occurred_at, trust, raw)"
            " VALUES (:u, 'notification', :e, :t, 'external', '{}'::jsonb) RETURNING id"
        ),
        {"u": user_id, "e": external_id or f"e-{amount}-{when.isoformat()}-{category}", "t": when},
    ).scalar_one()
    return transactions.record(
        user_id,
        session,
        occurred_at=when,
        amount=Decimal(amount),
        direction=Direction.DEBIT,
        kind=kind,
        channel="bank_sms",
        source_event_id=event_id,
        confidence=1.0,
        category=category if kind is TxnKind.EXPENSE else None,
    )


def alerts(session, user_id, *, now: datetime = MID_AUGUST):
    return BUDGET_ALERT.evaluate(RuleContext(user_id=user_id, session=session, now=now))


class TestSettingABudget:
    def test_a_total_budget_and_a_category_budget_coexist(self, pg_session, user_id):
        budgets.set_budget(user_id, pg_session, amount=Decimal("5000"))
        budgets.set_budget(user_id, pg_session, amount=Decimal("1500"), category="餐饮")

        rows = budgets.list_budgets(user_id, pg_session)
        assert [b.category for b in rows] == [None, "餐饮"]  # 总预算排最前

    def test_setting_it_again_changes_the_amount(self, pg_session, user_id):
        budgets.set_budget(user_id, pg_session, amount=Decimal("5000"))
        budgets.set_budget(user_id, pg_session, amount=Decimal("6000"))

        (only,) = budgets.list_budgets(user_id, pg_session)
        assert only.amount == Decimal("6000")

    def test_a_category_outside_the_enum_is_refused(self, pg_session, user_id):
        """**给一个枚举外的类目设预算,那条预算永远不会有消费落进来**,
        而它看起来在正常工作。"""
        with pytest.raises(BudgetError):
            budgets.set_budget(user_id, pg_session, amount=Decimal("500"), category="外卖")

    @pytest.mark.parametrize("amount", ["0", "-100"])
    def test_a_non_positive_budget_is_refused(self, pg_session, user_id, amount):
        with pytest.raises(BudgetError):
            budgets.set_budget(user_id, pg_session, amount=Decimal(amount))

    @pytest.mark.parametrize("threshold", ["0.05", "1.5"])
    def test_a_silly_threshold_is_refused(self, pg_session, user_id, threshold):
        """低于 0.1 等于月初就在报警,高于 1 等于这条线在超支之后 ——
        两种都让"接近"这条线失去意义。"""
        with pytest.raises(BudgetError):
            budgets.set_budget(
                user_id, pg_session, amount=Decimal("5000"),
                alert_threshold=Decimal(threshold),
            )

    def test_deleting_stops_the_alerts(self, pg_session, user_id):
        """**删了就不再预警**,而不是把额度设成很大 —— 后者会在报表上
        留下一条永远用不满的假预算。"""
        budgets.set_budget(user_id, pg_session, amount=Decimal("100"), category="餐饮")
        a_spend(pg_session, user_id, amount="200")
        assert alerts(pg_session, user_id)

        assert budgets.delete_budget(user_id, pg_session, category="餐饮") is True
        assert alerts(pg_session, user_id) == []


class TestWhatCountsAsSpending:
    def test_only_expenses(self, pg_session, user_id):
        """还款计入支出就是双重记账,消费那一刻已经记过一次。"""
        budgets.set_budget(user_id, pg_session, amount=Decimal("1000"), category="餐饮")
        a_spend(pg_session, user_id, amount="100", external_id="a")
        a_spend(pg_session, user_id, amount="5000", kind=TxnKind.REPAYMENT, external_id="b")

        (item,) = budgets.progress(user_id, pg_session, now=MID_AUGUST)
        assert item.spent == Decimal("100")

    def test_a_refund_does_not_offset_the_budget(self, pg_session, user_id):
        """**刻意的选择。** 退款可能发生在下个月,冲减会让"八月花了多少"
        这个数在九月还在变,而一个会变的历史数字没法用来判断超没超。"""
        budgets.set_budget(user_id, pg_session, amount=Decimal("1000"), category="餐饮")
        a_spend(pg_session, user_id, amount="300", external_id="a")
        a_spend(pg_session, user_id, amount="100", kind=TxnKind.REFUND, external_id="b")

        (item,) = budgets.progress(user_id, pg_session, now=MID_AUGUST)
        assert item.spent == Decimal("300")

    def test_last_month_does_not_count(self, pg_session, user_id):
        budgets.set_budget(user_id, pg_session, amount=Decimal("1000"), category="餐饮")
        a_spend(pg_session, user_id, amount="900", when=MID_AUGUST - timedelta(days=40))

        (item,) = budgets.progress(user_id, pg_session, now=MID_AUGUST)
        assert item.spent == Decimal("0")

    def test_the_total_budget_includes_uncategorized_spending(self, pg_session, user_id):
        """**没归类的那些也是花掉的钱。** 漏掉它们会让总预算永远看起来
        还有富余,而那正是超支最容易溜过去的地方。"""
        budgets.set_budget(user_id, pg_session, amount=Decimal("1000"))
        a_spend(pg_session, user_id, amount="400", category="餐饮", external_id="a")
        a_spend(pg_session, user_id, amount="300", category=None, external_id="b")

        (item,) = budgets.progress(user_id, pg_session, now=MID_AUGUST)
        assert item.spent == Decimal("700")

    def test_the_month_is_cut_in_local_time(self, pg_session, user_id):
        """一笔 8 月 31 日晚上的消费在 UTC 已经是 9 月 1 日。按 UTC 切的话
        八月少一笔、九月多一笔,**两个月的判断都因此错了**。"""
        budgets.set_budget(user_id, pg_session, amount=Decimal("1000"), category="餐饮")
        late = datetime(2026, 8, 31, 23, 30, tzinfo=SHANGHAI)
        a_spend(pg_session, user_id, amount="100", when=late)

        (august,) = budgets.progress(
            user_id, pg_session, now=datetime(2026, 8, 31, 23, 59, tzinfo=SHANGHAI)
        )
        assert august.spent == Decimal("100")

        (september,) = budgets.progress(
            user_id, pg_session, now=datetime(2026, 9, 1, 0, 30, tzinfo=SHANGHAI)
        )
        assert september.spent == Decimal("0")


class TestTheTwoLines:
    """**90% 和 100% 是两件不同的事。**"""

    def setup_budget(self, session, user_id, *, amount="1000", threshold="0.9"):
        return budgets.set_budget(
            user_id, session, amount=Decimal(amount), category="餐饮",
            alert_threshold=Decimal(threshold),
        )

    def test_nothing_is_said_below_the_threshold(self, pg_session, user_id):
        self.setup_budget(pg_session, user_id)
        a_spend(pg_session, user_id, amount="500")
        assert alerts(pg_session, user_id) == []

    def test_the_near_line_fires_at_the_threshold(self, pg_session, user_id):
        self.setup_budget(pg_session, user_id)
        a_spend(pg_session, user_id, amount="900")

        (reminder,) = alerts(pg_session, user_id)
        assert "快到了" in reminder.card.title
        assert reminder.dedup_key.endswith(":near")

    def test_the_over_line_fires_when_it_is_exceeded(self, pg_session, user_id):
        self.setup_budget(pg_session, user_id)
        a_spend(pg_session, user_id, amount="1100")

        (reminder,) = alerts(pg_session, user_id)
        assert "超了" in reminder.card.title
        assert reminder.dedup_key.endswith(":over")

    def test_over_is_not_also_near(self, pg_session, user_id):
        """**一笔消费同时触发两条提醒**只会让人觉得系统在重复说话。"""
        self.setup_budget(pg_session, user_id)
        a_spend(pg_session, user_id, amount="1100")
        assert len(alerts(pg_session, user_id)) == 1

    def test_the_two_lines_have_different_dedup_keys(self, pg_session, user_id):
        """**这一条是整片最要紧的。** 共用一个 key 的话,90% 那天发过之后,
        真正超支那天会被冷却期挡掉 —— 而那天恰恰最该说话。"""
        self.setup_budget(pg_session, user_id)
        a_spend(pg_session, user_id, amount="900", external_id="a")
        (near,) = alerts(pg_session, user_id)

        a_spend(pg_session, user_id, amount="200", external_id="b")
        (over,) = alerts(pg_session, user_id)

        assert near.dedup_key != over.dedup_key

    def test_exactly_at_the_budget_is_not_over_yet(self, pg_session, user_id):
        """花光和花超是两回事 —— 正好花完还没超。"""
        self.setup_budget(pg_session, user_id)
        a_spend(pg_session, user_id, amount="1000")

        (reminder,) = alerts(pg_session, user_id)
        assert reminder.dedup_key.endswith(":near")


class TestSayingItOnlyOnce:
    def test_the_cooldown_is_a_month_not_a_day(self, pg_session, user_id):
        """一条预算超了之后**每天都还是超着**。一天一条的话月底会连着提
        十几次同一件事,而 R4 说误报两次就足够让人永久关掉通知。"""
        budgets.set_budget(user_id, pg_session, amount=Decimal("100"), category="餐饮")
        a_spend(pg_session, user_id, amount="200")

        (reminder,) = alerts(pg_session, user_id)
        assert reminder.cooldown >= timedelta(days=28)

    def test_next_month_starts_over(self, pg_session, user_id):
        """周期在 dedup_key 里,所以下个月是新的一条。"""
        budgets.set_budget(user_id, pg_session, amount=Decimal("100"), category="餐饮")
        a_spend(pg_session, user_id, amount="200")

        august = alerts(pg_session, user_id)[0].dedup_key
        a_spend(
            pg_session, user_id, amount="200",
            when=datetime(2026, 9, 5, 12, 0, tzinfo=SHANGHAI), external_id="sep",
        )
        september = alerts(
            pg_session, user_id, now=datetime(2026, 9, 6, 12, 0, tzinfo=SHANGHAI)
        )[0].dedup_key

        assert august != september


class TestWithoutABudget:
    def test_no_budget_means_no_alerts(self, pg_session, user_id):
        """**不给一个"默认预算"** —— 猜出来的额度一定是错的,
        而一条错的预算发出的每一次提醒都是误报。"""
        a_spend(pg_session, user_id, amount="99999")
        assert alerts(pg_session, user_id) == []
        assert budgets.progress(user_id, pg_session, now=MID_AUGUST) == []

    def test_budgets_do_not_leak_between_users(self, pg_session, user_id):
        """铁律 1。预算是"这个人每月打算花多少",比一笔消费还私人。"""
        budgets.set_budget(user_id, pg_session, amount=Decimal("100"), category="餐饮")
        a_spend(pg_session, user_id, amount="200")

        other = "99999999-9999-9999-9999-999999999999"
        assert budgets.list_budgets(other, pg_session) == []
        assert alerts(pg_session, other) == []


def test_the_rule_is_registered():
    from lifein.rules.builtin import ALL_RULES

    assert BUDGET_ALERT in ALL_RULES


def test_it_starts_in_shadow(pg_session, user_id):
    """新规则默认 shadow(没有 `rule_state` 记录就是),**所以它上线的第一周
    不会打扰任何人** —— 这不是靠自觉,是靠默认值。"""
    from lifein.repos.rule_state import RuleMode, get_mode

    assert get_mode(user_id, pg_session, rule_id=BUDGET_ALERT.rule_id) is RuleMode.SHADOW
