"""主动提醒 job —— 跑规则,然后决定这句话到底说不说得出去。

**三道闸门,顺序有意为之:**

    1. 去重      同一条规则 + 同一件事,冷却期内只说一次
    2. 影子模式   规则不在 active,就只记录不推送
    3. 频率闸门   非用户触发的推送每天硬上限 3 条

去重排第一,因为它决定"这条本来算不算一次推送" —— 排在闸门后面的话,
同一场会被扫到四次就吃掉四次额度,而它本该只算一次。

影子排在闸门前面:影子记录**不占额度**。影子期的意义是统计"如果开了会推
多少条",让它去挤真实推送的额度,统计和体验就同时坏了。

**闸门由 `push_log` 计数强制,不靠规则自觉**(架构 §4)。超限的照样写一条
shadow 记录,并在 digest 里记下 `suppressed_by=rate_gate` —— 被闸门拦下和
规则处在影子模式都留下 shadow 行,但那两件事对"误报率算多少"意义完全不同,
不分开的话影子期的统计就是虚的。

**这个 job 不认领窗口。** 它每十几分钟跑一次、只看当下,补跑一个两小时前的
"提醒"没有任何意义 —— 那正是 `job_runs` 那套补偿机制不适用的场景。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from lifein.alerts import Alerter
from lifein.channels.base import Card, Channel
from lifein.repos import push_log, rule_state
from lifein.rules.base import Reminder, Rule, RuleContext
from lifein.rules.builtin import ALL_RULES

log = logging.getLogger(__name__)

DAILY_ACTIVE_LIMIT = 3
"""非用户触发的推送每天硬上限。产品定义 §5,不是可调参数。

调大它是在拿"用户还会不会看"换"多说几句话",而 R4 说误报两次就够让人
把通知关掉 —— 关掉之后说多少句都没用了。
"""


@dataclass(frozen=True)
class ReminderDeps:
    channel: Channel
    alerter: Alerter
    rules: Sequence[Rule] = ALL_RULES


@dataclass
class ReminderResult:
    pushed: int = 0
    shadowed: int = 0
    """规则还在影子模式,只记录没推。"""

    suppressed: int = 0
    """被频率闸门拦下的。这个数字持续大于 0 说明规则太吵。"""

    deduped: int = 0
    failed: int = 0

    def as_stats(self) -> dict:
        return {
            "pushed": self.pushed,
            "shadowed": self.shadowed,
            "suppressed": self.suppressed,
            "deduped": self.deduped,
            "failed": self.failed,
        }


def run_once(
    user_id: str,
    session: Session,
    *,
    deps: ReminderDeps,
    now: datetime,
) -> ReminderResult:
    result = ReminderResult()
    day_start = now - timedelta(days=1)

    for rule in deps.rules:
        mode = rule_state.get_mode(user_id, session, rule_id=rule.rule_id)
        if mode is rule_state.RuleMode.OFF:
            # 用户主动关掉的:连影子记录都不写。他要的是"别再算这件事了"
            continue

        try:
            reminders = rule.evaluate(RuleContext(user_id=user_id, session=session, now=now))
        except Exception as exc:  # noqa: BLE001
            # 一条规则炸了不该让别的规则不跑 —— 那会让"今天一条提醒都没有"
            # 看起来像"今天没事",而后者是这个系统最不该撒的谎
            log.exception("规则 %s 执行失败", rule.rule_id)
            deps.alerter.alert(f"规则 {rule.rule_id} 执行失败", f"{type(exc).__name__}: {exc}")
            result.failed += 1
            continue

        for reminder in reminders:
            _handle(
                user_id,
                session,
                deps=deps,
                rule=rule,
                reminder=reminder,
                mode=mode,
                now=now,
                day_start=day_start,
                result=result,
            )

    return result


def _with_off_hint(card: Card, rule_id: str) -> Card:
    """给推出去的提醒加一行"怎么关掉这一类"。

    [产品定义 §5](../../docs/01-product-spec.md#5-主动性双模式) 要求
    **每条主动推送都要能一键关闭该类规则**。P1 没有交互按钮
    (那是 P3 的审批卡片,ADR-001),所以"一键"在这一期的形态是
    **一条能直接抄走的命令**。

    要紧的不是它有多方便,而是**看到这条推送的当下就知道怎么关** ——
    等到去翻文档才找得到关法的时候,人已经先把整类通知关掉了
    ([R4](../../docs/05-risks.md#r4--主动推送误报摧毁信任))。
    """
    hint = f"不想再收这类:python -m lifein.admin rule-mode --rule {rule_id} --mode off"
    joined = "\n".join([card.footer, hint]) if card.footer else hint
    return replace(card, footer=joined)


def _handle(
    user_id: str,
    session: Session,
    *,
    deps: ReminderDeps,
    rule: Rule,
    reminder: Reminder,
    mode: rule_state.RuleMode,
    now: datetime,
    day_start: datetime,
    result: ReminderResult,
) -> None:
    # 第 1 道:去重。排最前面 —— 同一场会被扫到四次,不该吃掉四次额度
    if push_log.was_pushed_since(
        user_id,
        session,
        rule_id=rule.rule_id,
        dedup_key=reminder.dedup_key,
        since=now - reminder.cooldown,
    ):
        result.deduped += 1
        return

    # 第 2 道:影子模式。影子记录不占额度,否则影子期的统计和真实体验一起坏
    if mode is not rule_state.RuleMode.ACTIVE:
        push_log.record_push(
            user_id,
            session,
            channel=deps.channel.name,
            mode="shadow",
            card=reminder.card,
            delivered=False,
            rule_id=rule.rule_id,
            dedup_key=reminder.dedup_key,
        )
        result.shadowed += 1
        return

    # 第 3 道:频率闸门。由 push_log 计数强制,不靠规则自觉(架构 §4)
    if push_log.count_active_pushes_since(user_id, session, since=day_start) >= DAILY_ACTIVE_LIMIT:
        push_log.record_push(
            user_id,
            session,
            channel=deps.channel.name,
            mode="shadow",
            card=reminder.card,
            delivered=False,
            rule_id=rule.rule_id,
            dedup_key=reminder.dedup_key,
            extra={"suppressed_by": "rate_gate"},
        )
        result.suppressed += 1
        return

    card = _with_off_hint(reminder.card, rule.rule_id)

    try:
        delivery = deps.channel.send(user_id, card)
    except Exception as exc:  # noqa: BLE001
        # 推送失败也要写 push_log:"发过但没送到"是排查通道问题时唯一的线索。
        # 失败的不算额度(count 只数 delivered),所以下一轮它还有机会
        push_log.record_push(
            user_id,
            session,
            channel=deps.channel.name,
            mode="active",
            card=card,
            delivered=False,
            rule_id=rule.rule_id,
            dedup_key=reminder.dedup_key,
            error=f"{type(exc).__name__}: {exc}",
        )
        log.warning("提醒推送失败:rule=%s %s", rule.rule_id, exc)
        result.failed += 1
        return

    push_log.record_push(
        user_id,
        session,
        channel=delivery.channel,
        mode="active",
        card=card,
        delivered=True,
        rule_id=rule.rule_id,
        dedup_key=reminder.dedup_key,
    )
    result.pushed += 1
