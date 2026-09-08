"""对账单行的形状 —— **第 7、8 片的解析器要产出的就是这个**。

先写这个模块,是因为对账(第 6 片)必须知道自己在读什么,而两个解析器
(信用卡 PDF、支付宝/微信导出)会各写各的。没有共同落点的话,
第一个解析器写成什么样,第二个就得跟着长成什么样,而那不是设计,是巧合。

## 一行一条 raw_events

一封带 200 行的对账单要落成 200 条 `raw_events`,不是一条。
`UNIQUE (user_id, source_event_id)` 要求每笔交易有自己的来源事件,
共用一个 `source_event_id` 只能入账一笔(06 §2.6 第三层)。

`occurred_at` 填**那笔消费当时的日期**,不是收到对账单的日期。
它是归一化骨架的定义(06 §1),也是对账匹配的判据之一。

## raw 的形状

```json
{
  "channel": "statement",
  "statement": {"issuer": "cmb", "period": "2026-08", "line_no": 42},
  "parsed": {
    "amount": "38.50", "currency": "CNY", "direction": "debit",
    "account_hint": "1234", "merchant_raw": "星巴克(国贸店)",
    "order_no": "2026090812345", "kind": "expense",
    "text_redacted": true
  }
}
```

**`kind` 由解析器给,不由对账 job 猜。** 真实的对账单上有交易类型那一列
("消费""退货""还款"),解析器看得到它;对账 job 只看得到方向,
而 debit 既可能是消费也可能是还款 —— 把还款记成消费就是双重记账,
正是 03 那条"误记率 = 0"点名要挡的。解析器给不出来时留空,
那时对账 job 会走保守的那条路(见 `StatementLine.kind`)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from lifein.repos.raw_events import StoredEvent
from lifein.repos.transactions import Direction, TxnKind

log = logging.getLogger(__name__)

CHANNEL = "statement"
"""`raw.channel` 的取值。对账 job 靠它把对账单行和实时通知分开。"""


@dataclass(frozen=True)
class StatementLine:
    """对账单上的一行,已经解好。**金额一律是正数**,方向由 `direction` 表达。"""

    event_id: int
    amount: Decimal
    currency: str
    direction: Direction
    occurred_at: datetime
    account_hint: str | None = None
    merchant_raw: str | None = None
    order_no: str | None = None
    kind: TxnKind | None = None
    """解析器从"交易类型"那一列读到的。**给不出来就留空,不要按方向兜底。**

    debit 既可能是消费也可能是还款,而把还款记成消费就是双重记账 ——
    兜底会让这个错误发生得很安静。留空时对账 job 走保守的那条路。
    """

    issuer: str | None = None
    period: str | None = None


def from_event(stored: StoredEvent) -> StatementLine | None:
    """把一条对账单行的 `raw_events` 解回结构。**解不出来返回 None。**

    返回 None 的那些会被对账 job 跳过并计数 —— 它们意味着解析器产出的形状
    和这里对不上,而那是要修的 bug,不是要容忍的脏数据。所以不抛异常
    (一行坏的不该让整封对账单停下),但也不静默(计数会被报出来)。
    """
    event = stored.event
    raw = stored.raw or {}
    parsed = raw.get("parsed")
    if not isinstance(parsed, dict):
        log.warning("对账单行 %s 没有 parsed,跳过", stored.event_id)
        return None
    if event.amount is None:
        log.warning("对账单行 %s 没有金额,跳过", stored.event_id)
        return None

    statement = raw.get("statement") if isinstance(raw.get("statement"), dict) else {}
    return StatementLine(
        event_id=stored.event_id,
        # 金额、方向取归一化那份 —— 和实时那一路同一条规矩(铁律 9)
        amount=event.amount.value,
        currency=event.amount.currency,
        direction=event.amount.direction,
        occurred_at=event.occurred_at,
        account_hint=_text(parsed.get("account_hint")),
        merchant_raw=_text(parsed.get("merchant_raw")),
        order_no=_text(parsed.get("order_no")),
        kind=_kind(parsed.get("kind")),
        issuer=_text(statement.get("issuer")),
        period=_text(statement.get("period")),
    )


def _kind(value: object) -> TxnKind | None:
    if value is None:
        return None
    try:
        return TxnKind(str(value).strip().lower())
    except ValueError:
        # 枚举外的类型不猜一个最接近的 —— 和记账 agent 第 4 层同一条规矩
        log.warning("对账单行给了枚举外的 kind:%s", value)
        return None


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
