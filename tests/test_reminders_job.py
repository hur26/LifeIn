"""主动提醒的集成测试。需要真实 PostgreSQL(见 conftest.py)。

R4 说"主动推送误报摧毁信任",而这个 job 是唯一会主动说话的地方。
所以这一组几乎全在测**什么时候不说话**:

- 规则没转 active → 只记录
- 同一件事扫到多次 → 只说一次
- 今天已经说满 3 条 → 不说,但要留下痕迹
- 用户关掉了 → 连痕迹都不留
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from lifein.alerts import CollectingAlerter
from lifein.channels.base import Card, Delivery
from lifein.jobs.reminders import DAILY_ACTIVE_LIMIT, ReminderDeps, run_once
from lifein.repos import pending, push_log, rule_state, todos
from lifein.rules.base import Reminder, Rule
from lifein.rules.builtin import BACKLOG_REMIND_AT, PENDING_BACKLOG, UPCOMING_SCHEDULE

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
SOON = NOW + timedelta(minutes=20)
LATER = NOW + timedelta(hours=5)


class FakeChannel:
    name = "weixin"

    def __init__(self, boom: Exception | None = None) -> None:
        self.sent: list[Card] = []
        self._boom = boom

    def send(self, user_id, card):
        if self._boom:
            raise self._boom
        self.sent.append(card)
        return Delivery(channel=self.name, delivery_id=f"msg-{len(self.sent)}")


def deps(channel=None, rules=None) -> tuple[ReminderDeps, FakeChannel]:
    channel = channel or FakeChannel()
    return (
        ReminderDeps(
            channel=channel,
            alerter=CollectingAlerter(),
            rules=rules if rules is not None else (UPCOMING_SCHEDULE,),
        ),
        channel,
    )


def a_schedule(session, user_id: str, *, starts_at=SOON, title="项目周会"):
    return todos.create_todo(
        user_id,
        session,
        kind=todos.TodoKind.SCHEDULE,
        title=title,
        source=todos.TodoSource.AGENT,
        starts_at=starts_at,
        provenance=[1],
        created_by_agent="planner",
    )


def activate(session, user_id: str, rule_id: str) -> None:
    rule_state.set_mode(user_id, session, rule_id=rule_id, mode=rule_state.RuleMode.ACTIVE)


def logged(session, user_id: str):
    return session.execute(
        text("""
            SELECT rule_id, mode, delivered, payload_digest FROM push_log
             WHERE user_id = :u ORDER BY id
        """),
        {"u": user_id},
    ).all()


def test_new_rule_only_records_until_it_is_activated(pg_session, user_id):
    """没有 rule_state 记录就是 shadow。

    **忘记配置的后果是安静,不是打扰。** 反过来设计的话,某天有人加了条规则
    忘了说,用户第二天就被吵到 —— 而误报两次就足够让人关掉全部通知(R4)。
    """
    a_schedule(pg_session, user_id)
    d, channel = deps()

    result = run_once(user_id, pg_session, deps=d, now=NOW)

    assert result.shadowed == 1
    assert result.pushed == 0
    assert channel.sent == []
    [row] = logged(pg_session, user_id)
    assert row.mode == "shadow"
    assert row.rule_id == "upcoming_schedule"


def test_activated_rule_actually_pushes(pg_session, user_id):
    a_schedule(pg_session, user_id)
    activate(pg_session, user_id, "upcoming_schedule")
    d, channel = deps()

    result = run_once(user_id, pg_session, deps=d, now=NOW)

    assert result.pushed == 1
    assert channel.sent[0].summary.endswith("项目周会")
    [row] = logged(pg_session, user_id)
    assert (row.mode, row.delivered) == ("active", True)


def test_the_same_schedule_is_reminded_once(pg_session, user_id):
    """每十五分钟扫一次,没有去重就会把同一场会提醒到开完为止。"""
    a_schedule(pg_session, user_id)
    activate(pg_session, user_id, "upcoming_schedule")
    d, channel = deps()

    run_once(user_id, pg_session, deps=d, now=NOW)
    second = run_once(user_id, pg_session, deps=d, now=NOW + timedelta(minutes=15))

    assert second.deduped == 1
    assert second.pushed == 0
    assert len(channel.sent) == 1


def test_shadow_records_also_count_for_dedup(pg_session, user_id):
    """影子期要统计"这条规则会推多少次",重复计数会让误报率看起来偏低。

    而那正是决定要不要转 active 的那个数。
    """
    a_schedule(pg_session, user_id)
    d, _ = deps()

    run_once(user_id, pg_session, deps=d, now=NOW)
    second = run_once(user_id, pg_session, deps=d, now=NOW + timedelta(minutes=15))

    assert second.deduped == 1
    assert second.shadowed == 0


def test_rate_gate_stops_the_fourth_push_of_the_day(pg_session, user_id):
    """非用户触发的推送每天硬上限 3 条(产品定义 §5)。

    由 push_log 计数强制,不靠规则自觉。
    """
    for i in range(DAILY_ACTIVE_LIMIT + 1):
        a_schedule(pg_session, user_id, title=f"会议 {i}", starts_at=SOON + timedelta(minutes=i))
    activate(pg_session, user_id, "upcoming_schedule")
    d, channel = deps()

    result = run_once(user_id, pg_session, deps=d, now=NOW)

    assert result.pushed == DAILY_ACTIVE_LIMIT
    assert result.suppressed == 1
    assert len(channel.sent) == DAILY_ACTIVE_LIMIT

    suppressed = [r for r in logged(pg_session, user_id) if r.mode == "shadow"]
    # 被闸门拦下和"规则在影子模式"都留下 shadow 行,但两件事对误报率的意义
    # 完全不同 —— 所以要在 digest 里分得开
    assert suppressed[0].payload_digest["suppressed_by"] == "rate_gate"


def test_shadow_records_do_not_eat_the_daily_quota(pg_session, user_id):
    """影子期的意义是统计"如果开了会推多少条"。

    让它去挤真实推送的额度,统计和体验就同时坏了。
    """
    for i in range(DAILY_ACTIVE_LIMIT + 2):
        a_schedule(pg_session, user_id, title=f"会议 {i}", starts_at=SOON + timedelta(minutes=i))
    d, _ = deps()

    result = run_once(user_id, pg_session, deps=d, now=NOW)

    assert result.shadowed == DAILY_ACTIVE_LIMIT + 2
    assert result.suppressed == 0


def test_user_switched_it_off_leaves_no_trace(pg_session, user_id):
    """用户主动关掉的,连影子记录都不写 —— 他要的是"别再算这件事了"。"""
    a_schedule(pg_session, user_id)
    rule_state.set_mode(
        user_id, pg_session, rule_id="upcoming_schedule", mode=rule_state.RuleMode.OFF
    )
    d, channel = deps()

    result = run_once(user_id, pg_session, deps=d, now=NOW)

    assert (result.pushed, result.shadowed, result.deduped) == (0, 0, 0)
    assert logged(pg_session, user_id) == []
    assert channel.sent == []


def test_far_off_schedule_is_not_reminded_yet(pg_session, user_id):
    a_schedule(pg_session, user_id, starts_at=LATER)
    activate(pg_session, user_id, "upcoming_schedule")
    d, _ = deps()

    assert run_once(user_id, pg_session, deps=d, now=NOW).pushed == 0


def test_cancelled_schedule_is_not_reminded(pg_session, user_id):
    created = a_schedule(pg_session, user_id)
    todos.set_status(
        user_id, pg_session, todo_id=created.id, status=todos.TodoStatus.CANCELLED
    )
    activate(pg_session, user_id, "upcoming_schedule")
    d, _ = deps()

    assert run_once(user_id, pg_session, deps=d, now=NOW).pushed == 0


def test_push_failure_is_logged_and_does_not_eat_the_quota(pg_session, user_id):
    """"发过但没送到"是排查通道问题时唯一的线索。

    失败的不算额度,所以下一轮它还有机会。
    """
    a_schedule(pg_session, user_id)
    activate(pg_session, user_id, "upcoming_schedule")
    d, _ = deps(channel=FakeChannel(boom=RuntimeError("通道挂了")))

    result = run_once(user_id, pg_session, deps=d, now=NOW)

    assert result.failed == 1
    [row] = logged(pg_session, user_id)
    assert (row.mode, row.delivered) == ("active", False)
    delivered = push_log.count_active_pushes_since(
        user_id, pg_session, since=NOW - timedelta(days=1)
    )
    assert delivered == 0


def test_one_broken_rule_does_not_silence_the_others(pg_session, user_id):
    """一条规则炸了不该让别的规则不跑。

    那会让"今天一条提醒都没有"看起来像"今天没事",而后者是这个系统
    最不该撒的谎。
    """

    def explode(_ctx):
        raise RuntimeError("规则写错了")

    broken = Rule(rule_id="broken", description="必然失败", evaluate=explode)
    a_schedule(pg_session, user_id)
    activate(pg_session, user_id, "upcoming_schedule")
    alerter = CollectingAlerter()
    d = ReminderDeps(
        channel=FakeChannel(), alerter=alerter, rules=(broken, UPCOMING_SCHEDULE)
    )

    result = run_once(user_id, pg_session, deps=d, now=NOW)

    assert result.failed == 1
    assert result.pushed == 1
    assert alerter.alerts


def test_backlog_rule_fires_once_a_day(pg_session, user_id):
    for i in range(BACKLOG_REMIND_AT):
        pending.enqueue(
            user_id,
            pg_session,
            agent="planner",
            kind=pending.PendingKind.CALENDAR_EVENT,
            target_table="todos",
            payload={"title": f"待确认 {i}"},
            reason=pending.PendingReason.LOW_CONFIDENCE,
            now=NOW,
        )
    activate(pg_session, user_id, "pending_backlog")
    d, channel = deps(rules=(PENDING_BACKLOG,))

    first = run_once(user_id, pg_session, deps=d, now=NOW)
    # 队列不会自己变短,所以这条规则每次扫描都会命中 —— 冷却期是唯一的刹车
    second = run_once(user_id, pg_session, deps=d, now=NOW + timedelta(hours=2))

    assert first.pushed == 1
    assert second.deduped == 1
    assert len(channel.sent) == 1


def test_backlog_rule_stays_quiet_below_the_threshold(pg_session, user_id):
    pending.enqueue(
        user_id,
        pg_session,
        agent="planner",
        kind=pending.PendingKind.TASK,
        target_table="todos",
        payload={"title": "只有一条"},
        reason=pending.PendingReason.LOW_CONFIDENCE,
        now=NOW,
    )
    activate(pg_session, user_id, "pending_backlog")
    d, _ = deps(rules=(PENDING_BACKLOG,))

    assert run_once(user_id, pg_session, deps=d, now=NOW).pushed == 0


def test_reminders_are_isolated_per_user(pg_session, user_id):
    other = str(uuid.uuid4())
    pg_session.execute(
        text("INSERT INTO users (id, display_name, wecom_userid) VALUES (:id, :n, :w)"),
        {"id": other, "n": "另一个人", "w": f"other-{other[:8]}"},
    )
    a_schedule(pg_session, user_id)
    activate(pg_session, other, "upcoming_schedule")
    d, channel = deps()

    assert run_once(other, pg_session, deps=d, now=NOW).pushed == 0
    assert channel.sent == []


def test_reminder_dedup_is_per_rule(pg_session, user_id):
    """两条规则针对同一件事时,互不影响对方的冷却期。"""
    created = a_schedule(pg_session, user_id)
    other_rule = Rule(
        rule_id="other",
        description="另一条规则,针对同一条日程",
        evaluate=lambda ctx: [
            Reminder(card=Card(title="另一条", summary="x"), dedup_key=created.id)
        ],
    )
    activate(pg_session, user_id, "upcoming_schedule")
    activate(pg_session, user_id, "other")
    d, channel = deps(rules=(UPCOMING_SCHEDULE, other_rule))

    result = run_once(user_id, pg_session, deps=d, now=NOW)

    assert result.pushed == 2
    assert len(channel.sent) == 2


class TestTheOffSwitch:
    """产品定义 §5:**每条主动推送都要能一键关闭该类规则。**

    P1 没有交互按钮(那是 P3 的审批卡片),所以"一键"在这一期的形态是
    推送里带一条能直接抄走的命令。要紧的不是它多方便,而是**看到推送的当下
    就知道怎么关** —— 等到要去翻文档才找得到关法时,人已经把整类通知关掉了(R4)。
    """

    def test_every_active_push_says_how_to_turn_it_off(self, pg_session, user_id):
        a_schedule(pg_session, user_id)
        rule_state.set_mode(
            user_id, pg_session, rule_id=UPCOMING_SCHEDULE.rule_id, mode=rule_state.RuleMode.ACTIVE
        )
        reminder_deps, channel = deps()

        run_once(user_id, pg_session, deps=reminder_deps, now=NOW)

        (card,) = channel.sent
        assert card.footer and UPCOMING_SCHEDULE.rule_id in card.footer
        assert "rule-mode" in card.footer

    def test_turning_a_rule_off_is_not_the_same_as_shadow(self, pg_session, user_id):
        """`off` 是用户主动关掉的那一档,不该再被自动转回 active(06 §2.12)。"""
        a_schedule(pg_session, user_id)
        rule_state.set_mode(
            user_id, pg_session, rule_id=UPCOMING_SCHEDULE.rule_id, mode=rule_state.RuleMode.OFF
        )
        reminder_deps, channel = deps()

        result = run_once(user_id, pg_session, deps=reminder_deps, now=NOW)

        assert channel.sent == []
        # 关掉的连影子记录都不留 —— 那是"你不想要这类",不是"还在观察"
        assert result.shadowed == 0
        assert push_log.list_since(user_id, pg_session, since=NOW - timedelta(days=1)) == []


class TestShadowReview:
    """03 要求"先跑一周影子模式统计,再决定是否转 active"。

    统计的前提是看得见 —— 在 `list_since` 之前,影子记录只进得去、出不来。
    """

    def test_shadow_pushes_can_be_listed_for_review(self, pg_session, user_id):
        a_schedule(pg_session, user_id, title="项目周会")
        reminder_deps, channel = deps()  # 默认 shadow

        run_once(user_id, pg_session, deps=reminder_deps, now=NOW)

        assert channel.sent == []  # 影子期一条都不推
        records = push_log.list_since(
            user_id, pg_session, since=NOW - timedelta(days=1), mode="shadow"
        )
        (record,) = records
        assert record.rule_id == UPCOMING_SCHEDULE.rule_id
        assert record.delivered is False
        # 日志里**没有正文**(审计记摘要不记原文),所以复盘靠 dedup_key
        # 在读的时候把那条日程解出来 —— admin rules --detail 做的就是这件事
        assert record.dedup_key is not None
        assert todos.get_todo(user_id, pg_session, todo_id=record.dedup_key).title == "项目周会"

    def test_filtering_by_rule(self, pg_session, user_id):
        a_schedule(pg_session, user_id)
        reminder_deps, _ = deps()
        run_once(user_id, pg_session, deps=reminder_deps, now=NOW)

        assert push_log.list_since(
            user_id, pg_session, since=NOW - timedelta(days=1), rule_id=PENDING_BACKLOG.rule_id
        ) == []
        assert len(
            push_log.list_since(
                user_id,
                pg_session,
                since=NOW - timedelta(days=1),
                rule_id=UPCOMING_SCHEDULE.rule_id,
            )
        ) == 1
