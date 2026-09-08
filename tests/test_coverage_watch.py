"""覆盖率巡检(P2 第 11 片)。需要真实 PostgreSQL:全是聚合查询。

R8 说覆盖率是数据源格式变动的**早期信号** —— 某个来源突然解析不出来,
会先表现为覆盖率下降,而不是报错。所以这一组盯的是两件事:

1. **该说话时说话**:四个数各自越线时都要有一条人话
2. **不该说话时闭嘴**:样本太小、一切正常时一条都不发

第二件比第一件要紧。这个 job 每天跑一次,而 R4 说误报两次就足够让人
永久关掉通知 —— **一条每天都来的假告警,会让真告警到来时没有人看**。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from lifein.jobs import coverage_watch
from lifein.jobs.coverage_watch import (
    BACKLOG_CEILING,
    COVERAGE_FLOOR,
    FAILED_PARSE_CEILING,
    MIN_TRANSACTIONS,
    CoverageDeps,
)
from lifein.repos import transactions
from lifein.repos.transactions import Direction, Stage, TxnKind

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
RECENTLY = NOW - timedelta(days=3)


class RecordingAlerter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def alert(self, title: str, body: str) -> None:
        self.sent.append((title, body))


def a_txn(session, user_id, *, index: int, stage: Stage = Stage.REALTIME):
    event_id = session.execute(
        text(
            "INSERT INTO raw_events (user_id, source, external_id, occurred_at, trust, raw)"
            " VALUES (:u, 'notification', :e, :t, 'external', '{}'::jsonb) RETURNING id"
        ),
        {"u": user_id, "e": f"e-{index}", "t": RECENTLY},
    ).scalar_one()
    return transactions.record(
        user_id,
        session,
        occurred_at=RECENTLY,
        amount=Decimal("10.00") + index,
        direction=Direction.DEBIT,
        kind=TxnKind.EXPENSE,
        channel="bank_sms",
        source_event_id=event_id,
        confidence=1.0,
        category="餐饮",
        stage=stage,
    )


def a_failed_event(session, user_id, *, index: int):
    """归一化失败的那种。**原文留着**,解析器改好能重跑(R8)。"""
    session.execute(
        text(
            "INSERT INTO raw_events (user_id, source, external_id, occurred_at, trust,"
            " raw, normalize_error) VALUES (:u, 'notification', :e, :t, 'external',"
            " '{}'::jsonb, '认不出这条通知')"
        ),
        {"u": user_id, "e": f"bad-{index}", "t": RECENTLY},
    )


def a_bookkeeping_run(session, user_id, *, by_rule: int, by_llm: int, day: int = 3):
    start = NOW - timedelta(days=day)
    session.execute(
        text(
            "INSERT INTO job_runs (user_id, job_name, window_start, window_end, status, stats)"
            " VALUES (:u, 'bookkeeping', :s, :e, 'succeeded', CAST(:stats AS JSONB))"
        ),
        {
            "u": user_id,
            "s": start,
            "e": start + timedelta(days=1),
            "stats": json.dumps({"categorized_by_rule": by_rule, "categorized_by_llm": by_llm}),
        },
    )


def run(session, user_id, *, alerter=None):
    return coverage_watch.run_once(
        user_id, session, deps=CoverageDeps(alerter=alerter or RecordingAlerter()), now=NOW
    )


class TestSayingNothing:
    """**比"该说话时说话"更要紧的一半。**"""

    def test_a_brand_new_user_gets_no_alert(self, pg_session, user_id):
        alerter = RecordingAlerter()
        report = run(pg_session, user_id, alerter=alerter)

        assert report.healthy
        assert alerter.sent == []

    def test_a_small_sample_has_no_coverage_at_all(self, pg_session, user_id):
        """**三笔里有一笔没对上就是 67%**,而它什么都说明不了 ——
        只会每天来一条假告警,直到有人把整个告警通道关掉。"""
        for i in range(3):
            a_txn(pg_session, user_id, index=i)

        report = run(pg_session, user_id)
        assert report.coverage is None
        assert report.healthy

    def test_a_healthy_month_says_nothing(self, pg_session, user_id):
        for i in range(MIN_TRANSACTIONS):
            a_txn(pg_session, user_id, index=i, stage=Stage.RECONCILED)
        a_bookkeeping_run(pg_session, user_id, by_rule=90, by_llm=10)

        alerter = RecordingAlerter()
        report = run(pg_session, user_id, alerter=alerter)

        assert report.coverage == 1.0
        assert report.healthy
        assert alerter.sent == []

    def test_old_data_falls_out_of_the_window(self, pg_session, user_id):
        """看的是最近三十天。**上个季度的一次故障不该一直告警。**"""
        a_bookkeeping_run(pg_session, user_id, by_rule=0, by_llm=100, day=200)

        report = run(pg_session, user_id)
        assert report.llm_share is None  # 窗口里一条记录都没有
        assert report.healthy


class TestSayingSomething:
    def test_low_coverage_says_what_it_probably_means(self, pg_session, user_id):
        """**一条只有数字的告警,读的人还得自己回忆阈值是多少。**"""
        for i in range(MIN_TRANSACTIONS):
            stage = Stage.RECONCILED if i < 10 else Stage.REALTIME
            a_txn(pg_session, user_id, index=i, stage=stage)

        alerter = RecordingAlerter()
        report = run(pg_session, user_id, alerter=alerter)

        assert report.coverage == 0.5
        assert not report.healthy
        (_, body) = alerter.sent[0]
        assert "覆盖率" in body
        assert "通知文案" in body  # 说清楚这意味着什么

    def test_exactly_at_the_floor_is_fine(self, pg_session, user_id):
        """阈值是"低于才说",不是"到了就说" —— 正好达标的那个月不该被打扰。"""
        total = MIN_TRANSACTIONS
        for i in range(total):
            stage = Stage.RECONCILED if i < int(total * COVERAGE_FLOOR) else Stage.REALTIME
            a_txn(pg_session, user_id, index=i, stage=stage)

        assert run(pg_session, user_id).healthy

    def test_failed_parses_are_the_most_direct_signal(self, pg_session, user_id):
        """R8 最直接的表现。**它从来不会自己变好**,所以阈值定得低。"""
        for i in range(FAILED_PARSE_CEILING + 1):
            a_failed_event(pg_session, user_id, index=i)

        report = run(pg_session, user_id)
        assert report.failed_parses == FAILED_PARSE_CEILING + 1
        assert any("归一化失败" in c for c in report.concerns)
        assert any("能重跑" in c for c in report.concerns)  # 告诉人下一步能做什么

    def test_a_high_llm_share_points_at_the_rules_table(self, pg_session, user_id):
        """ADR-008 说这个数不随时间下降就是 bug,不是常态。"""
        a_bookkeeping_run(pg_session, user_id, by_rule=10, by_llm=90)

        report = run(pg_session, user_id)
        assert report.llm_share == 0.9
        assert any("规则表" in c for c in report.concerns)

    def test_the_share_comes_from_what_the_job_counted(self, pg_session, user_id):
        """**从 transactions 反推是推不出来的**:一条归好类的记录上看不出
        当初是谁归的。记账 job 每一轮都数好了,直接用那个数。"""
        a_bookkeeping_run(pg_session, user_id, by_rule=30, by_llm=10, day=2)
        a_bookkeeping_run(pg_session, user_id, by_rule=30, by_llm=10, day=3)

        report = run(pg_session, user_id)
        assert (report.by_rule, report.by_llm) == (60, 20)

    def test_a_big_backlog_names_both_possible_causes(self, pg_session, user_id):
        """"判据太保守"和"没人看那个队列了"是两件事,而这个数字都可能是它们。"""
        from lifein.repos import pending

        for _ in range(BACKLOG_CEILING + 1):
            pending.enqueue(
                user_id,
                pg_session,
                agent="bookkeeper",
                kind=pending.PendingKind.TRANSACTION,
                target_table="transactions",
                payload={"amount": "1.00"},
                reason=pending.PendingReason.LOW_CONFIDENCE,
                confidence=0.5,
                source_event_id=None,
                now=NOW,
            )

        report = run(pg_session, user_id)
        assert any("待确认" in c for c in report.concerns)

    def test_everything_wrong_is_one_alert_not_four(self, pg_session, user_id):
        """**四条问题发四封邮件,第二天就没人看了。** 一次说完。"""
        for i in range(MIN_TRANSACTIONS):
            a_txn(pg_session, user_id, index=i)
        for i in range(FAILED_PARSE_CEILING + 1):
            a_failed_event(pg_session, user_id, index=i)
        a_bookkeeping_run(pg_session, user_id, by_rule=1, by_llm=99)

        alerter = RecordingAlerter()
        report = run(pg_session, user_id, alerter=alerter)

        assert len(report.concerns) >= 3
        assert len(alerter.sent) == 1

    def test_the_alert_says_it_is_a_trend_not_an_error(self, pg_session, user_id):
        """收到的人第一反应会是"哪里崩了" —— 说清楚不是。"""
        for i in range(FAILED_PARSE_CEILING + 1):
            a_failed_event(pg_session, user_id, index=i)

        alerter = RecordingAlerter()
        run(pg_session, user_id, alerter=alerter)

        (_, body) = alerter.sent[0]
        assert "不是错误" in body


def test_it_does_not_reach_across_users(pg_session, user_id):
    """铁律 1。别人的覆盖率不该影响你这边说不说话。"""
    for i in range(FAILED_PARSE_CEILING + 1):
        a_failed_event(pg_session, user_id, index=i)

    other = "99999999-9999-9999-9999-999999999999"
    alerter = RecordingAlerter()
    report = run(pg_session, other, alerter=alerter)

    assert report.healthy
    assert alerter.sent == []
