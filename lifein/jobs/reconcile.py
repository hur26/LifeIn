"""对账 job —— 两阶段入账的第二阶段(ADR-012)。

    对账单行(raw_events, channel=statement)
        → 已经对过的 → 什么都不做(幂等)
        → 匹配上实时那一笔 → 回填真实商户名、订单号,重新归类
        → 匹配不上         → 补成一笔新的(stage=reconciled),计入覆盖率

**这个 job 存在的理由是实时通知里的商户名没用。** "财付通""支付宝"是收钱
的通道,不是你消费的地方,拿它归类只会得到一堆"其他"。真实商户名只有
对账单给得出,所以归类要跑两次(ADR-008),而第二次才是有价值的那次 ——
`merchant_rules` 真正的沉淀从这里开始。

## 它不按时间窗跑

别的 job 都认领 `job_runs` 的窗口,这个不。一行对账单的 `occurred_at`
是**那笔消费当时的日期**(上个月),而它落地是本月的事 ——
按时间窗取的 job 永远看不到它们。所以这里改成"处理过没有":
一行要么变成了一笔交易,要么被回填到了实时那一笔上,两种之外没有第三种。
**那两道同时也是幂等键**,所以重跑多少次都是同一个结果,
而这正是 ADR-012 要的:"重复入账比漏记更糟 —— 漏记你会发现,重复不会。"

## 回填不过网关,追溯靠行上那一列

网关的判据是"谁提的"(06 §6.6),而回填谁都没提:它是把一份权威数据补到
一条已经存在、已经审计过的记录上,金额、时间、方向一个都不改。那笔交易当初
入账时过了网关,`tool_calls` 里那条记录还在。

**回填本身的追溯写在行上**:`matched_statement_event_id` 指向具体是哪一行
对账单改的它。这比补一条 `tool_calls` 记录准 —— 那条记录只能说"对了一批账",
而这一列能回答"这笔的商户名是从哪儿来的"。

## 它不调模型

归类只查 `merchant_rules`。查不到就**留着实时那一遍给的临时分类**,
不为此打一次模型:对账单一次几百行,而其中大部分商户在规则表里已经有了。
长尾那部分由下个月同一个商户再出现时的实时链路去归 —— 那一路本来就要
调模型判定,顺带就把分类给了,不用在这里多花一次(铁律 9)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from lifein.alerts import Alerter
from lifein.governance.gateway import CallContext, Gateway
from lifein.models.normalized import Trust
from lifein.repos import merchant_rules, raw_events, transactions
from lifein.repos.tool_calls import PostgresAuditSink
from lifein.repos.transactions import Stage, TxnKind
from lifein.sources import statement
from lifein.sources.statement import StatementLine

log = logging.getLogger(__name__)

JOB_NAME = "reconcile"
AGENT = "bookkeeper"
TOOL = "txn.record"

MAX_LINES_PER_RUN = 1000
"""一次最多处理多少行。一封对账单几百行,两三封同时到也不该把一次运行拖到
半小时 —— 剩下的下次接着处理,因为"处理过没有"本来就是可续的。"""

GatewayFactory = Callable[[str, Session], Gateway]


def default_gateway(user_id: str, session: Session) -> Gateway:
    return Gateway(PostgresAuditSink(user_id, session))


@dataclass(frozen=True)
class ReconcileDeps:
    alerter: Alerter
    gateway_factory: GatewayFactory = default_gateway


@dataclass
class ReconcileResult:
    lines_seen: int = 0
    backfilled: int = 0
    """匹配上实时那一笔并回填了商户名的。**这个数越大越好。**"""

    recorded_new: int = 0
    """匹配不上、补成新记录的。**它意味着实时那一路漏了一笔**,
    而漏记是这条链路唯一不会自己暴露的错误 —— 覆盖率统计(第 11 片)盯的就是它。"""

    skipped_done: int = 0
    unparsable: int = 0
    """解不出形状的行。**不是脏数据,是解析器和 `sources/statement.py` 对不上**,
    要修。所以它单独计数,不混进别的数字里。"""

    rules_learned: int = 0
    recategorized: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float | None:
        """实时通道的覆盖率:对账单里有多少笔曾在实时通道出现过(03 的指标)。

        一行都没有时返回 None 而不是 1.0 —— **"全覆盖"和"这个月还没对过账"
        不该长成同一个数字**。
        """
        total = self.backfilled + self.recorded_new
        return self.backfilled / total if total else None

    def as_stats(self) -> dict:
        return {
            "lines_seen": self.lines_seen,
            "backfilled": self.backfilled,
            "recorded_new": self.recorded_new,
            "skipped_done": self.skipped_done,
            "unparsable": self.unparsable,
            "rules_learned": self.rules_learned,
            "recategorized": self.recategorized,
            "coverage": self.coverage,
        }


def run_once(
    user_id: str,
    session: Session,
    *,
    deps: ReconcileDeps,
    limit: int = MAX_LINES_PER_RUN,
) -> ReconcileResult:
    """把还没对过账的对账单行处理完。

    **没有窗口可认领,也不看时间。** 别的 job 都要 `now`,这个不要 ——
    它处理的是"还没处理过的",而那个集合和现在几点没有关系。
    收一个用不上的 `now` 会让人以为重跑的结果跟时间有关,而它没有。
    """
    result = ReconcileResult()
    try:
        _reconcile(user_id, session, deps=deps, limit=limit, result=result)
    except Exception as exc:  # noqa: BLE001
        log.exception("对账 job 异常")
        result.error = f"{type(exc).__name__}: {exc}"
        deps.alerter.alert("对账异常终止", result.error)

    if result.unparsable:
        # 单独告警:它不是"这个月账单少几笔",是解析器产出的形状不对
        message = f"{result.unparsable} 行对账单解不出形状,解析器和 statement.py 对不上"
        result.warnings.append(message)
        deps.alerter.alert("对账单解析形状不符", message)
    return result


def _reconcile(
    user_id: str,
    session: Session,
    *,
    deps: ReconcileDeps,
    limit: int,
    result: ReconcileResult,
) -> None:
    lines = raw_events.fetch_unreconciled_statement_lines(
        user_id, session, channel=statement.CHANNEL, limit=limit
    )
    result.lines_seen = len(lines)
    if not lines:
        return

    gateway = deps.gateway_factory(user_id, session)
    for stored in lines:
        line = statement.from_event(stored)
        if line is None:
            result.unparsable += 1
            continue

        # 幂等第一道。取的时候已经滤过一遍,这里再看一次是为了防同一次运行里
        # 前面那行刚把它对掉(同一封对账单里出现两行完全一样的并不罕见)
        if transactions.is_reconciled(user_id, session, statement_event_id=line.event_id):
            result.skipped_done += 1
            continue

        match = transactions.find_reconcilable(
            user_id,
            session,
            amount=line.amount,
            occurred_at=line.occurred_at,
            account_hint=line.account_hint,
        )
        if match is None:
            _record_new(user_id, session, gateway, line, result=result)
        else:
            _backfill(user_id, session, match, line, result=result)


def _backfill(
    user_id: str,
    session: Session,
    match: transactions.Transaction,
    line: StatementLine,
    *,
    result: ReconcileResult,
) -> None:
    """回填。**这一步不过网关。**

    网关的判据是"谁提的"(06 §6.6),而这一步谁都没提:它是把一份权威数据
    补到一条已经存在、已经审计过的记录上,金额、时间、方向一个都不改。
    真正的写入(那一笔交易)当初已经过了网关,`tool_calls` 里那条记录还在。
    """
    decided = merchant_rules.categorize(user_id, session, merchant=line.merchant_raw)
    if decided.by_rule and decided.category != match.category:
        result.recategorized += 1

    after = transactions.backfill(
        user_id,
        session,
        txn_id=match.id,
        statement_event_id=line.event_id,
        merchant_raw=line.merchant_raw,
        order_no=line.order_no,
        # 规则没命中就留着实时那一遍的临时分类:空着会让这笔从报表里掉出去
        category=decided.category,
    )
    if after is None:
        # 并发下另一遍抢先对掉了。不是错误,幂等在起作用
        result.skipped_done += 1
        return

    result.backfilled += 1
    # **规则表真正的沉淀从这里开始**:现在的商户名才是真的(ADR-008)
    if after.category and not decided.by_rule:
        learned = merchant_rules.remember(
            user_id, session, merchant=line.merchant_raw, category=after.category
        )
        if learned is not None:
            result.rules_learned += 1


def _record_new(
    user_id: str,
    session: Session,
    gateway: Gateway,
    line: StatementLine,
    *,
    result: ReconcileResult,
) -> None:
    """匹配不上的补为新记录(ADR-012)。**这一步过网关** —— 它是一次新的入账。

    `kind` 只认解析器给的。给不出来就跳过并计数:debit 既可能是消费也可能是
    还款,按方向兜底会把还款记成消费,而那正是 03 那条"误记率 = 0"点名要挡的。
    宁可这一笔不入账 —— 它会留在覆盖率的分母外面,而漏一笔查得出来。
    """
    if line.kind is None:
        result.warnings.append(f"对账单行 {line.event_id} 没有交易类型,不入账")
        log.info("对账单行 %s 没给 kind,跳过入账", line.event_id)
        return

    decided = merchant_rules.categorize(user_id, session, merchant=line.merchant_raw)
    ctx = CallContext(
        user_id=user_id,
        agent=AGENT,
        trust=Trust.EXTERNAL,
        source_event_id=line.event_id,
        session=session,
    )
    try:
        value = gateway.call(
            ctx,
            TOOL,
            {
                "occurred_at": line.occurred_at.isoformat(),
                "amount": str(line.amount),
                "currency": line.currency,
                "direction": line.direction.value,
                "kind": line.kind.value,
                "channel": statement.CHANNEL,
                "source_event_id": line.event_id,
                # 对账单是金额权威源(ADR-012 那张表),不是推断出来的
                "confidence": 1.0,
                "merchant_raw": line.merchant_raw,
                "category": decided.category if line.kind is TxnKind.EXPENSE else None,
                "account_hint": line.account_hint,
                "order_no": line.order_no,
                "stage": Stage.RECONCILED.value,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("补录对账单行失败,跳过这一行:%s", exc)
        result.warnings.append(f"补录失败:{exc}")
        return

    if not value["created"]:
        # 按返回值计数,不按"调用没报错"计数。补录理论上一定是新建
        # (stage=reconciled 不走跨渠道合并,而 source_event_id 是这一行独有的),
        # 真出现了别的结果说明有 bug,而算成 recorded_new 会把覆盖率算低,
        # 让人以为实时那一路漏得比实际多
        log.warning("补录对账单行 %s 没有新建:%s", line.event_id, value)
        result.warnings.append(f"补录未新建:事件 {line.event_id}")
        result.skipped_done += 1
        return

    result.recorded_new += 1
