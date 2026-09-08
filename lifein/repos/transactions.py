"""`transactions` 的读写 —— P2 的落地表。

**这一层最容易做错的是去重**([06 §2.6](../../docs/06-data-model.md#26-去重的两个层次)):
它是两个不同的问题,不许用一个字段解决。

| 问题 | 手段 |
| --- | --- |
| 同一条通知被重复上报(采集器重试、网络重投) | `UNIQUE (user_id, source_event_id)` —— 数据库约束 |
| 同一笔交易在多个渠道各发一条(支付宝通知 + 银行短信) | **5 分钟窗口查询判定**,不是约束 |

第二条**绝不能做成唯一约束**:便利店连买两次同价的东西是真实存在的,
约束会把第二笔悄悄吃掉 —— 而"少了一笔"比"多了一笔"更难发现,
因为对账时你只会觉得这个月花得少。

所以跨渠道合并是**查询 + 可回溯的记录**:被合并掉的那条事件 id 进
`merged_from_event_ids`,任何时候都能问"这一笔是从哪几条通知拼出来的"。

**`kind` 是四层防误判的落地点。** 只有 `expense` 与 `income` 进统计;
`repayment`(信用卡还款)算进支出会双重记账 —— 消费那一刻已经记过一次了。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

MERGE_WINDOW = timedelta(minutes=5)
"""跨渠道合并的时间窗。07 的 `TXN_DEDUP_WINDOW_S` 默认就是 300 秒。

窗口开大会把两笔真实的连续消费并成一笔;开小会让支付宝和银行短信各记一次。
五分钟是"同一笔交易的两条通知"能差出的最大延迟,不是拍出来的。
"""


class Direction(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class TxnKind(StrEnum):
    """**这个枚举就是防误判的输出空间。**

    LLM 只能在这几个里选一个(06 §2.5 的 CHECK 也只认这几个),
    而"选不出来"意味着进待确认,不是硬塞一个 expense。
    """

    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"
    """转账。既不是支出也不是收入 —— 钱只是换了个地方。"""

    REFUND = "refund"
    REPAYMENT = "repayment"
    """信用卡还款。**记成支出就是双重记账** —— 消费那一刻已经记过一次。"""

    @property
    def counts_as_spending(self) -> bool:
        return self is TxnKind.EXPENSE


class Stage(StrEnum):
    REALTIME = "realtime"
    """实时通道落下的:金额、时间、卡号后四位可信,商户名多半是代收机构。"""

    RECONCILED = "reconciled"
    """月度账单回填过的:商户名和订单号才是真的(ADR-012 的两阶段入账)。"""


class TransactionError(ValueError):
    """写不进去。都在进库前抛,带得出人话的原因。"""


@dataclass(frozen=True)
class Transaction:
    id: int
    occurred_at: datetime
    amount: Decimal
    currency: str
    direction: Direction
    kind: TxnKind
    merchant_raw: str | None
    category: str | None
    account_hint: str | None
    channel: str
    stage: Stage
    source_event_id: int
    merged_from_event_ids: list[int]
    confidence: float

    @property
    def counts_as_spending(self) -> bool:
        return self.kind.counts_as_spending


_COLUMNS = """
    id, occurred_at, amount, currency, direction, kind, merchant_raw, category,
    account_hint, channel, stage, source_event_id, merged_from_event_ids, confidence
"""

_INSERT = text(f"""
    INSERT INTO transactions
        (user_id, occurred_at, amount, currency, direction, kind, merchant_raw,
         category, account_hint, channel, stage, source_event_id, confidence)
    VALUES
        (:user_id, :occurred_at, :amount, :currency, :direction, :kind, :merchant_raw,
         :category, :account_hint, :channel, :stage, :source_event_id, :confidence)
    ON CONFLICT (user_id, source_event_id) DO NOTHING
    RETURNING {_COLUMNS}
""")

_SELECT_BY_EVENT = text(f"""
    SELECT {_COLUMNS} FROM transactions
     WHERE user_id = :user_id AND source_event_id = :source_event_id
""")

_FIND_MERGEABLE = text(f"""
    SELECT {_COLUMNS}
      FROM transactions
     WHERE user_id = :user_id
       AND amount = :amount
       AND channel <> :channel
       AND occurred_at BETWEEN :window_start AND :window_end
       -- CAST 不能省:裸参数直接跟 IS NULL 比,Postgres 推不出它的类型,
       -- 报 AmbiguousParameter 而不是"你写错了"。
       -- 另:注释里也不能出现冒号开头的记号 —— text() 不认 SQL 注释,
       -- 会把它当成一个必须传值的绑定参数(这一条也是踩出来的)
       AND (
            account_hint IS NULL
            OR CAST(:account_hint AS TEXT) IS NULL
            OR account_hint = CAST(:account_hint AS TEXT)
       )
     ORDER BY abs(EXTRACT(EPOCH FROM (occurred_at - :occurred_at)))
     LIMIT 1
""")

_MERGE = text(f"""
    UPDATE transactions
       SET merged_from_event_ids = merged_from_event_ids || :event_id,
           merchant_raw = COALESCE(merchant_raw, :merchant_raw),
           account_hint = COALESCE(account_hint, :account_hint),
           confidence = GREATEST(confidence, :confidence)
     WHERE user_id = :user_id AND id = :txn_id
 RETURNING {_COLUMNS}
""")

_LIST_BETWEEN = text(f"""
    SELECT {_COLUMNS} FROM transactions
     WHERE user_id = :user_id
       AND occurred_at >= :start AND occurred_at < :end
     ORDER BY occurred_at DESC
     LIMIT :limit
""")

_SPENDING_BY_CATEGORY = text("""
    SELECT COALESCE(category, '未归类') AS category, sum(amount) AS total, count(*) AS count
      FROM transactions
     WHERE user_id = :user_id
       AND kind = 'expense'
       AND occurred_at >= :start AND occurred_at < :end
     GROUP BY 1
     ORDER BY total DESC
""")


@dataclass(frozen=True)
class RecordResult:
    """写入的结果。

    `merged_into` 不为 None 说明**这条通知被合并进了已有的一笔** ——
    不是失败,是跨渠道去重生效了(06 §2.6)。
    """

    transaction: Transaction
    created: bool
    merged_into: int | None = None
    duplicate: bool = False
    """同一条事件被重复上报。库上的唯一键挡的就是它。"""


def record(
    user_id: str,
    session: Session,
    *,
    occurred_at: datetime,
    amount: Decimal,
    direction: Direction,
    kind: TxnKind,
    channel: str,
    source_event_id: int,
    confidence: float,
    merchant_raw: str | None = None,
    category: str | None = None,
    account_hint: str | None = None,
    currency: str = "CNY",
    stage: Stage = Stage.REALTIME,
    merge_window: timedelta = MERGE_WINDOW,
) -> RecordResult:
    """记一笔。**两层去重都在这里发生,顺序不能换。**

    1. 先找**跨渠道的同一笔**(时间窗查询)—— 找到就合并,不新建
    2. 再插入,靠 `UNIQUE (user_id, source_event_id)` 挡住重复上报

    顺序反过来的话,支付宝那条会先插进去,再也不会去找银行短信那条 ——
    两条各记一笔,而月底你只会看到总额多了一倍。
    """
    if amount <= 0:
        # 金额的正负由 direction 表达,不由符号 —— 混着来的话
        # "退款 -50" 和 "支出 50" 在求和时会互相抵消,而它们是两件事
        raise TransactionError(f"金额必须为正,方向由 direction 表达:{amount}")
    if not 0.0 <= confidence <= 1.0:
        raise TransactionError(f"confidence 必须在 0 和 1 之间:{confidence}")
    if occurred_at.tzinfo is None:
        raise TransactionError("occurred_at 必须带时区")

    existing = session.execute(
        _SELECT_BY_EVENT, {"user_id": user_id, "source_event_id": source_event_id}
    ).first()
    if existing:
        # 同一条通知又送上来一次(采集器重试)。什么都不做
        return RecordResult(transaction=_to_txn(existing), created=False, duplicate=True)

    mergeable = session.execute(
        _FIND_MERGEABLE,
        {
            "user_id": user_id,
            "amount": amount,
            "channel": channel,
            "account_hint": account_hint,
            "occurred_at": occurred_at,
            "window_start": occurred_at - merge_window,
            "window_end": occurred_at + merge_window,
        },
    ).first()

    if mergeable:
        merged = session.execute(
            _MERGE,
            {
                "user_id": user_id,
                "txn_id": mergeable.id,
                "event_id": [source_event_id],
                # 补上对方缺的:银行短信有卡号没商户,支付宝通知反过来
                "merchant_raw": merchant_raw,
                "account_hint": account_hint,
                "confidence": confidence,
            },
        ).one()
        log.info(
            "跨渠道合并:事件 %s 并入交易 %s(%s + %s)",
            source_event_id,
            mergeable.id,
            mergeable.channel,
            channel,
        )
        return RecordResult(
            transaction=_to_txn(merged), created=False, merged_into=int(mergeable.id)
        )

    row = session.execute(
        _INSERT,
        {
            "user_id": user_id,
            "occurred_at": occurred_at,
            "amount": amount,
            "currency": currency,
            "direction": direction.value,
            "kind": kind.value,
            "merchant_raw": merchant_raw,
            "category": category,
            "account_hint": account_hint,
            "channel": channel,
            "stage": stage.value,
            "source_event_id": source_event_id,
            "confidence": confidence,
        },
    ).first()

    if row is None:
        # 并发下另一个事务先插进去了。按重复处理,不抛异常
        again = session.execute(
            _SELECT_BY_EVENT, {"user_id": user_id, "source_event_id": source_event_id}
        ).one()
        return RecordResult(transaction=_to_txn(again), created=False, duplicate=True)

    return RecordResult(transaction=_to_txn(row), created=True)


def list_between(
    user_id: str, session: Session, *, start: datetime, end: datetime, limit: int = 200
) -> list[Transaction]:
    rows = session.execute(
        _LIST_BETWEEN, {"user_id": user_id, "start": start, "end": end, "limit": limit}
    ).all()
    return [_to_txn(row) for row in rows]


def spending_by_category(
    user_id: str, session: Session, *, start: datetime, end: datetime
) -> list[tuple[str, Decimal, int]]:
    """按类目汇总**支出**。预算预警与月度报告都读它。

    只数 `kind='expense'`:还款、转账、退款进来会让每一个数字都不可信,
    而那种错在报表上看不出来 —— 它只是让金额偏大。
    """
    rows = session.execute(
        _SPENDING_BY_CATEGORY, {"user_id": user_id, "start": start, "end": end}
    ).all()
    return [(row.category, row.total, row.count) for row in rows]


def get_by_event(user_id: str, session: Session, *, source_event_id: int) -> Transaction | None:
    row = session.execute(
        _SELECT_BY_EVENT, {"user_id": user_id, "source_event_id": source_event_id}
    ).first()
    return _to_txn(row) if row else None


def _to_txn(row) -> Transaction:
    return Transaction(
        id=row.id,
        occurred_at=row.occurred_at,
        amount=row.amount,
        currency=row.currency,
        direction=Direction(row.direction),
        kind=TxnKind(row.kind),
        merchant_raw=row.merchant_raw,
        category=row.category,
        account_hint=row.account_hint,
        channel=row.channel,
        stage=Stage(row.stage),
        source_event_id=row.source_event_id,
        merged_from_event_ids=list(row.merged_from_event_ids or []),
        confidence=float(row.confidence),
    )
