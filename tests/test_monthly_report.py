"""月度报告(P2 第 10 片)。

分两半,和这一片的设计一样:

- **数字**在 `repos/reports.py` 里用 SQL 算,要真库
- **评语**由 agent 写,用假 LLM

盯的是同一件事的两面。数字那半:**算错的月度报告和算对的长得一模一样**,
没有人会去核对"餐饮 1234.56"是不是真等于那几十笔之和。评语那半:
模型最典型的错法是编一个"比上月多了 300",而那句话读起来比真话还顺 ——
所以复核是"每个数字都必须在给它的统计里逐字出现过"。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from lifein.agents.monthly_report import (
    MAX_NOTES,
    MonthlyInput,
    MonthlyOutput,
    MonthlyReportFailed,
    build_summary,
    to_card,
    write_notes,
)
from lifein.repos import reports, transactions
from lifein.repos.reports import CategoryLine, MerchantLine, MonthlyReport
from lifein.repos.transactions import Direction, Stage, TxnKind

SHANGHAI = timezone(timedelta(hours=8))
IN_AUGUST = datetime(2026, 8, 15, 12, 0, tzinfo=SHANGHAI)
IN_JULY = datetime(2026, 7, 15, 12, 0, tzinfo=SHANGHAI)


class FakeLLM:
    def __init__(self, notes: list[str] | str) -> None:
        self.payload = notes
        self.seen: list = []

    def chat(self, messages):
        self.seen.append(messages)
        content = (
            self.payload
            if isinstance(self.payload, str)
            else json.dumps({"notes": self.payload}, ensure_ascii=False)
        )
        return _Response(content)


class _Response:
    def __init__(self, content: str) -> None:
        self._content = content
        self.prompt_tokens = 200
        self.completion_tokens = 50

    def as_json(self):
        return json.loads(self._content)


def a_report(**overrides) -> MonthlyReport:
    payload = {
        "period": "2026-08",
        "period_start": datetime(2026, 8, 1, tzinfo=SHANGHAI),
        "period_end": datetime(2026, 9, 1, tzinfo=SHANGHAI),
        "total": Decimal("3120.50"),
        "count": 42,
        "last_total": Decimal("2800.00"),
        "categories": [
            CategoryLine("餐饮", Decimal("1200.00"), 20, Decimal("900.00")),
            CategoryLine("交通", Decimal("420.50"), 12, Decimal("500.00")),
        ],
        "merchants": [MerchantLine("星巴克", Decimal("380.00"), 8)],
        "uncategorized": Decimal("0"),
        "reconciled_ratio": 0.8,
    }
    payload.update(overrides)
    return MonthlyReport(**payload)


def run(notes, report=None):
    llm = FakeLLM(notes)
    result = write_notes(MonthlyInput(report=report or a_report()), llm=llm)
    return result.output, llm


class TestWhatGoesToTheModel:
    def test_the_numbers_are_already_computed(self):
        """**给的是算好的数,不是原始交易。** 原始交易一送进去,模型就会
        自己求和,而它求和会错 —— 那正是这一片要避免的事。"""
        summary = build_summary(a_report())

        assert "3120.50" in summary
        assert "餐饮:1200.00 元" in summary
        assert "上个月 900.00 元" in summary

    def test_the_model_is_told_not_to_compute(self):
        _, llm = run(["餐饮比上个月多了不少"])
        prompt = json.dumps(llm.seen[0], ensure_ascii=False)
        assert "不要重新计算" in prompt
        assert "不要自己算差值" in prompt

    def test_an_empty_month_never_calls_the_model(self):
        """铁律 9:没有素材就不该花一次调用。"""
        llm = FakeLLM([])
        result = write_notes(
            MonthlyInput(report=a_report(total=Decimal("0"), count=0, categories=[])),
            llm=llm,
        )
        assert result.output.notes == []
        assert llm.seen == []


class TestTheReview:
    """**模型说了什么,和它说得对不对,是两件事。**"""

    def test_a_note_quoting_a_real_number_survives(self):
        output, _ = run(["餐饮 1200.00 元,是花得最多的一类"])
        assert len(output.notes) == 1

    def test_an_invented_number_is_dropped(self):
        """**这是这条链路最典型的错法**,而那句话读起来比真话还顺。"""
        output, _ = run(["餐饮比上个月多了 300.00 元"])

        assert output.notes == []
        assert output.dropped_invented == 1

    def test_a_note_without_numbers_is_fine(self):
        """不引用数字的观察是最安全的那种,不该被误伤。"""
        output, _ = run(["交通比上个月省了一些"])
        assert output.notes == ["交通比上个月省了一些"]

    def test_a_category_with_no_spending_is_dropped(self):
        """模型很爱说"你这个月娱乐花得少",而那一类可能根本没有记录。"""
        output, _ = run(["娱乐这个月几乎没花钱"])

        assert output.notes == []
        assert output.dropped_unknown_category == 1

    def test_too_many_notes_are_truncated(self):
        """**一份月报里五条已经算多** —— 再多没人读完,而没人读完的报告
        等于没发。截断而不是重试:重试要多花一次调用换一个更啰嗦的结果。"""
        output, _ = run(["餐饮花得多", "交通省了", "商户集中", "笔数不少", "还行", "补充"])
        assert len(output.notes) == MAX_NOTES

    def test_the_check_is_deliberately_over_eager(self):
        """**这条记下来是因为它是一个已知的代价,不是 bug。**

        判据是"note 里的每个数字都要在统计里逐字出现过",所以模型自己数出来的
        真话("有 2 类超了预算")也会被丢掉 —— 统计里没有那个 2。

        放宽的办法是给小整数开口子,但那正好是"多了 3 倍""涨了 2 成"这类
        编造最爱用的形状。**宁可丢掉几句对的,不要放进一句错的**:
        错的那句读起来比真话还顺,而没有人会去核对。
        """
        output, _ = run(["这个月有 2 类花得比上月多"])
        assert output.dropped_invented == 1

    def test_a_long_note_is_cut_not_dropped(self):
        output, _ = run(["很长" * 100])
        assert len(output.notes) == 1
        assert len(output.notes[0]) <= 60

    def test_bad_model_output_is_a_failure_not_a_guess(self):
        with pytest.raises(MonthlyReportFailed):
            run('["不是对象"]')


class TestTheCard:
    def test_the_numbers_come_from_the_report_not_the_notes(self):
        """**卡片这一层也不做任何计算**,免得又多一处可能算错的地方。"""
        card = to_card(a_report(), MonthlyOutput(notes=["餐饮花得最多"]))

        assert "3120.50" in card.summary
        assert "比上月多 320.50 元" in card.summary

    def test_no_previous_month_means_no_comparison(self):
        """第一个月的"环比 -100%"是假的 —— 上个月没有记录不等于没花钱。"""
        card = to_card(a_report(last_total=None), MonthlyOutput())
        assert "比上月" not in card.summary

    def test_uncategorized_money_gets_its_own_line(self):
        """混进"其他"的话,报告会声称自己看懂了这些钱,而实际上没有。"""
        card = to_card(a_report(uncategorized=Decimal("500.00")), MonthlyOutput())
        headings = [section.heading for section in card.sections]
        assert "还没归类" in headings

    def test_the_footer_says_how_much_was_reconciled(self):
        """**它是"这份报告可不可信"的唯一提示** —— 覆盖率低意味着有些消费
        根本没进账本,而报告本身看不出这一点。"""
        assert "80%" in to_card(a_report(), MonthlyOutput()).footer
        assert "还没有对过账" in to_card(
            a_report(reconciled_ratio=None), MonthlyOutput()
        ).footer


@pytest.mark.integration
class TestTheNumbers:
    """数字那一半。需要真实 PostgreSQL:全是聚合查询。"""

    def a_spend(
        self,
        session,
        user_id,
        *,
        amount: str,
        category: str | None = "餐饮",
        merchant: str | None = "星巴克",
        kind: TxnKind = TxnKind.EXPENSE,
        when: datetime = IN_AUGUST,
        stage: Stage = Stage.REALTIME,
        external_id: str | None = None,
    ):
        event_id = session.execute(
            text(
                "INSERT INTO raw_events (user_id, source, external_id, occurred_at,"
                " trust, raw) VALUES (:u, 'notification', :e, :t, 'external',"
                " '{}'::jsonb) RETURNING id"
            ),
            {
                "u": user_id,
                "e": external_id or f"{amount}-{category}-{when.isoformat()}-{kind.value}",
                "t": when,
            },
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
            merchant_raw=merchant,
            stage=stage,
        )

    def test_the_month_is_the_month_now_falls_in(self, pg_session, user_id):
        self.a_spend(pg_session, user_id, amount="100", when=IN_AUGUST)
        self.a_spend(pg_session, user_id, amount="200", when=IN_JULY)

        report = reports.monthly(user_id, pg_session, now=IN_AUGUST)
        assert report.period == "2026-08"
        assert (report.total, report.count) == (Decimal("100"), 1)

    def test_last_month_is_the_previous_calendar_month(self, pg_session, user_id):
        """**不是"今天减 30 天"** —— 那在三月一日会退到一月,
        而那份报告看起来完全正常,只是拿一月的数在比。"""
        self.a_spend(pg_session, user_id, amount="100", when=IN_AUGUST)
        self.a_spend(pg_session, user_id, amount="250", when=IN_JULY)

        report = reports.monthly(user_id, pg_session, now=IN_AUGUST)
        assert report.last_total == Decimal("250")
        assert report.delta == Decimal("-150")

    def test_no_previous_month_is_none_not_zero(self, pg_session, user_id):
        self.a_spend(pg_session, user_id, amount="100", when=IN_AUGUST)

        report = reports.monthly(user_id, pg_session, now=IN_AUGUST)
        assert report.last_total is None
        assert report.delta is None

    def test_only_expenses_count(self, pg_session, user_id):
        """和预算那一片同一个口径。两处不一样的话,报告说"花了 3000"、
        预算说"超了 500/2000",而用户没法知道该信哪个。"""
        self.a_spend(pg_session, user_id, amount="100", external_id="a")
        self.a_spend(
            pg_session, user_id, amount="5000", kind=TxnKind.REPAYMENT, external_id="b"
        )

        assert reports.monthly(user_id, pg_session, now=IN_AUGUST).total == Decimal("100")

    def test_uncategorized_is_reported_apart_from_the_categories(self, pg_session, user_id):
        self.a_spend(pg_session, user_id, amount="100", category="餐饮", external_id="a")
        self.a_spend(pg_session, user_id, amount="70", category=None, external_id="b")

        report = reports.monthly(user_id, pg_session, now=IN_AUGUST)
        assert report.uncategorized == Decimal("70")
        assert [c.category for c in report.categories] == ["餐饮"]
        assert report.total == Decimal("170")  # 但总额把它算进去了

    def test_categories_carry_last_month(self, pg_session, user_id):
        self.a_spend(pg_session, user_id, amount="300", category="餐饮", when=IN_AUGUST)
        self.a_spend(pg_session, user_id, amount="200", category="餐饮", when=IN_JULY)
        self.a_spend(pg_session, user_id, amount="50", category="交通", when=IN_AUGUST)

        report = reports.monthly(user_id, pg_session, now=IN_AUGUST)
        by_name = {c.category: c for c in report.categories}

        assert by_name["餐饮"].delta == Decimal("100")
        # 上个月这一类没有记录 —— **是 None 不是 0**,"没花"和"没记"是两件事
        assert by_name["交通"].last_total is None
        assert by_name["交通"].delta is None

    def test_merchants_are_ranked_and_capped(self, pg_session, user_id):
        for i, amount in enumerate(["500", "400", "300", "200", "100", "50"]):
            self.a_spend(
                pg_session, user_id, amount=amount, merchant=f"商户{i}", external_id=f"m{i}"
            )

        report = reports.monthly(user_id, pg_session, now=IN_AUGUST, top_merchants=3)
        assert [m.merchant for m in report.merchants] == ["商户0", "商户1", "商户2"]

    def test_the_reconciled_ratio(self, pg_session, user_id):
        self.a_spend(pg_session, user_id, amount="100", stage=Stage.REALTIME, external_id="a")
        self.a_spend(pg_session, user_id, amount="200", stage=Stage.RECONCILED, external_id="b")

        assert reports.monthly(user_id, pg_session, now=IN_AUGUST).reconciled_ratio == 0.5

    def test_an_empty_month_is_empty_not_zero_filled(self, pg_session, user_id):
        report = reports.monthly(user_id, pg_session, now=IN_AUGUST)

        assert report.is_empty
        assert report.reconciled_ratio is None  # 一笔都没有时不是 100%

    def test_reports_do_not_leak_between_users(self, pg_session, user_id):
        """铁律 1。一份月度报告是这个人一整月的行踪。"""
        self.a_spend(pg_session, user_id, amount="100")
        other = "99999999-9999-9999-9999-999999999999"

        assert reports.monthly(other, pg_session, now=IN_AUGUST).is_empty


@pytest.mark.integration
class TestTheJob:
    """**一个月只发一次**,靠 `job_runs` 而不是靠"今天是不是一号"。

    日期判断在两种情况下出错:进程在一号那天没起来(整月补不回来),
    以及一号那天重启了两次(发两遍)。而一份月报发两遍比不发更让人烦 ——
    第二遍和第一遍一模一样,收到的人会以为系统坏了。
    """

    def deps(self, notes=("餐饮花得最多",), sender=None):
        from lifein.jobs.monthly_report import MonthlyDeps

        return MonthlyDeps(llm=FakeLLM(list(notes)), channel=sender or Sent(), alerter=Quiet())

    def test_it_reports_last_month_not_this_one(self, pg_session, user_id):
        """**不是"最近 30 天"** —— 一份跨月的报告没法回答"这个月花超了没有"。"""
        from lifein.jobs import monthly_report as job

        TestTheNumbers().a_spend(pg_session, user_id, amount="100", when=IN_JULY)
        TestTheNumbers().a_spend(pg_session, user_id, amount="900", when=IN_AUGUST)

        channel = Sent()
        result = job.run_once(
            user_id, pg_session, deps=self.deps(sender=channel),
            now=datetime(2026, 9, 3, 9, 0, tzinfo=SHANGHAI),
        )

        assert result.period == "2026-08"
        assert result.delivered is True
        assert "900" in channel.cards[0].summary

    def test_the_second_run_in_the_same_month_sends_nothing(self, pg_session, user_id):
        from lifein.jobs import monthly_report as job

        TestTheNumbers().a_spend(pg_session, user_id, amount="100", when=IN_AUGUST)
        now = datetime(2026, 9, 3, 9, 0, tzinfo=SHANGHAI)

        first = job.run_once(user_id, pg_session, deps=self.deps(), now=now)
        channel = Sent()
        second = job.run_once(
            user_id, pg_session, deps=self.deps(sender=channel),
            now=datetime(2026, 9, 4, 9, 0, tzinfo=SHANGHAI),
        )

        assert first.delivered is True
        assert second.skipped is True
        assert channel.cards == []

    def test_an_empty_month_sends_nothing(self, pg_session, user_id):
        """**空报告比不发更伤信任** —— 它会让人以为记账在正常工作,
        而实际上一条都没采到。"""
        from lifein.jobs import monthly_report as job

        channel = Sent()
        result = job.run_once(
            user_id, pg_session, deps=self.deps(sender=channel),
            now=datetime(2026, 9, 3, 9, 0, tzinfo=SHANGHAI),
        )

        assert result.no_transactions is True
        assert channel.cards == []

    def test_a_model_failure_alerts_and_sends_nothing(self, pg_session, user_id):
        from lifein.jobs import monthly_report as job
        from lifein.jobs.monthly_report import MonthlyDeps

        TestTheNumbers().a_spend(pg_session, user_id, amount="100", when=IN_AUGUST)
        alerter, channel = Loud(), Sent()
        result = job.run_once(
            user_id,
            pg_session,
            deps=MonthlyDeps(llm=FakeLLM('["不是对象"]'), channel=channel, alerter=alerter),
            now=datetime(2026, 9, 3, 9, 0, tzinfo=SHANGHAI),
        )

        assert result.error
        assert alerter.sent  # 静默失败等于每个月都以为发过了
        assert channel.cards == []


class Sent:
    def __init__(self, name: str = "weixin") -> None:
        # push_log 的 channel 有 CHECK 约束,只认真实存在的通道名
        self.name = name
        self.cards: list = []

    def send(self, user_id: str, card):
        from lifein.channels.base import Delivery

        self.cards.append(card)
        return Delivery(channel=self.name, delivery_id="x")


class Quiet:
    def alert(self, title: str, body: str) -> None:
        pass


class Loud:
    def __init__(self) -> None:
        self.sent: list = []

    def alert(self, title: str, body: str) -> None:
        self.sent.append((title, body))


class TestTheLongEmailVersion:
    """03 那句"企微卡片 + 邮件长版"。

    **长版存在的理由是推送通道有长度上限**:企微卡片 2048 字节、微信 4000 字,
    而一份完整的月报有十个类目加五个商户,一定会被截断 —— 截断之后
    看起来仍然是一份完整的报告,只是后面几类没了。
    """

    def a_full_report(self) -> MonthlyReport:
        return a_report(
            categories=[
                CategoryLine(name, Decimal("100.00"), 3, Decimal("90.00"))
                for name in ("餐饮", "交通", "购物", "居住", "通信", "娱乐", "医疗")
            ],
            merchants=[
                MerchantLine(f"商户{i}", Decimal("50.00"), 2) for i in range(5)
            ],
            uncategorized=Decimal("30.00"),
        )

    def test_the_long_card_keeps_every_category(self):
        """短版只列前六个 —— 那是给卡片留的余量,而邮件不需要那个余量。"""
        from lifein.agents.monthly_report import to_long_card

        short = to_card(self.a_full_report(), MonthlyOutput())
        long = to_long_card(self.a_full_report(), MonthlyOutput())

        short_lines = next(s for s in short.sections if s.heading == "按类目").lines
        long_lines = next(s for s in long.sections if s.heading == "按类目").lines
        assert len(short_lines) == 6
        assert len(long_lines) == 7

    def test_the_long_card_lists_merchants(self):
        """商户榜在短版里根本没有位置,而它是看出"钱去哪了"最快的一栏。"""
        from lifein.agents.monthly_report import to_long_card

        headings = [s.heading for s in to_long_card(self.a_full_report(), MonthlyOutput()).sections]
        assert "花得最多的几家" in headings

    def test_both_versions_quote_the_same_numbers(self):
        """**两份的数字来自同一个 report。** 出现"卡片上说 3120、邮件里说 3121"
        那种事,比缺几行糟得多 —— 那时你不知道该信哪一个。"""
        from lifein.agents.monthly_report import to_long_card

        report = self.a_full_report()
        assert str(report.total) in to_card(report, MonthlyOutput()).summary
        assert str(report.total) in to_long_card(report, MonthlyOutput()).summary


@pytest.mark.integration
class TestSendingBothVersions:
    def test_the_email_gets_the_long_one(self, pg_session, user_id):
        from lifein.jobs import monthly_report as job
        from lifein.jobs.monthly_report import MonthlyDeps

        TestTheNumbers().a_spend(pg_session, user_id, amount="100", when=IN_AUGUST)
        push, mail = Sent(), Sent(name="email")

        result = job.run_once(
            user_id,
            pg_session,
            deps=MonthlyDeps(
                llm=FakeLLM(["餐饮花得最多"]), channel=push, alerter=Quiet(), email=mail
            ),
            now=datetime(2026, 9, 3, 9, 0, tzinfo=SHANGHAI),
        )

        assert result.delivered and result.long_version_sent
        assert "完整版" in mail.cards[0].title
        assert "完整版" not in push.cards[0].title

    def test_no_email_configured_still_sends_the_card(self, pg_session, user_id):
        """**没配邮件是一个缺口,不是一个故障。** 卡片照发。"""
        from lifein.jobs import monthly_report as job
        from lifein.jobs.monthly_report import MonthlyDeps

        TestTheNumbers().a_spend(pg_session, user_id, amount="100", when=IN_AUGUST)
        push = Sent()

        result = job.run_once(
            user_id,
            pg_session,
            deps=MonthlyDeps(llm=FakeLLM(["还行"]), channel=push, alerter=Quiet(), email=None),
            now=datetime(2026, 9, 3, 9, 0, tzinfo=SHANGHAI),
        )

        assert result.delivered
        assert result.long_version_sent is False

    def test_the_long_version_failing_does_not_fail_the_job(self, pg_session, user_id):
        """卡片已经送到了。**为长版把整个月报标成失败,下个月会重发一遍。**"""
        from lifein.jobs import monthly_report as job
        from lifein.jobs.monthly_report import MonthlyDeps

        TestTheNumbers().a_spend(pg_session, user_id, amount="100", when=IN_AUGUST)
        push, mail = Sent(), Broken()

        result = job.run_once(
            user_id,
            pg_session,
            deps=MonthlyDeps(
                llm=FakeLLM(["还行"]), channel=push, alerter=Quiet(), email=mail
            ),
            now=datetime(2026, 9, 3, 9, 0, tzinfo=SHANGHAI),
        )

        assert result.delivered is True
        assert result.error is None
        assert result.warnings  # 但要说出来

    def test_an_email_only_setup_does_not_send_twice(self, pg_session, user_id):
        """主通道就是邮件时,卡片那份已经是全的 —— 再发一封只是打扰。"""
        from lifein.jobs import monthly_report as job
        from lifein.jobs.monthly_report import MonthlyDeps

        TestTheNumbers().a_spend(pg_session, user_id, amount="100", when=IN_AUGUST)
        mail = Sent(name="email")

        job.run_once(
            user_id,
            pg_session,
            deps=MonthlyDeps(llm=FakeLLM(["还行"]), channel=mail, alerter=Quiet(), email=mail),
            now=datetime(2026, 9, 3, 9, 0, tzinfo=SHANGHAI),
        )
        assert len(mail.cards) == 1


class Broken:
    name = "email"

    def send(self, user_id: str, card):
        raise RuntimeError("SMTP 连不上")
