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
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from lifein.repos.raw_events import StoredEvent
from lifein.repos.transactions import Direction, TxnKind

log = logging.getLogger(__name__)

CHANNEL = "statement"
"""`raw.channel` 的取值。对账 job 靠它把对账单行和实时通知分开。"""


@dataclass(frozen=True)
class StatementRow:
    """对账单上的一行,**刚从解析器出来、还没入库**。

    第 7 片(PDF)和第 8 片(支付宝/微信导出)都产出它,所以它在这里而不在
    某一个解析器里 —— **放在先写的那个里面,第二个就只能跟着长成那个样子**,
    而那不是设计,是先来后到。

    和下面的 `StatementLine` 只差一个 `event_id`:那个是入库之后的形态,
    两者中间隔着"一行落成一条 raw_events"(06 §2.6 第三层)。
    """

    occurred_on: date
    amount: Decimal
    direction: Direction
    merchant_raw: str | None = None
    account_hint: str | None = None
    order_no: str | None = None
    kind: TxnKind | None = None
    """从"交易类型"那一列读到的。**读不到就留空,不按方向兜底** ——
    理由见 `sources/statement.py` 里 `StatementLine.kind` 的说明。"""

    raw_cells: tuple[str, ...] = ()
    """这一行的原始单元格。取错了的时候,它是唯一能看出错在哪一格的东西。"""


_REFUND_MARKERS = ("退货", "退款", "冲正", "撤销")
_REPAYMENT_MARKERS = ("还款", "自动还款", "网上还款")
_TRANSFER_MARKERS = ("转账", "转入", "转出")
_INCOME_MARKERS = ("收入", "工资", "退税", "红包")
_EXPENSE_MARKERS = ("消费", "支付", "支出", "扣款")
"""认类型用的字眼。**"利息"故意不在里面。**

储蓄卡上的利息是收入,信用卡上的利息是费用 —— 同两个字,方向相反。
猜错的代价不对称:认成收入会让月度收入凭空多一笔,而那种错误在报表上
看起来像"这个月进账不错"。认不出就是 None,让它走待确认。
"""

_AMOUNT = re.compile(
    r"^\s*(?P<sign>[-+])?\s*(?:CNY|RMB|¥|￥)?\s*(?P<value>[\d,]+\.?\d{0,2})\s*$"
)
_CARD_TAIL = re.compile(r"(\d{4})\D*$")
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%m-%d",
    "%m/%d",
    "%m.%d",
    "%Y年%m月%d日",
)


def parse_amount(text: str) -> tuple[Decimal, Direction] | None:
    """金额 → (正数, 方向)。**负号只表示方向,不进金额。**

    库里的 `amount` 一律是正数,正负由 `direction` 表达 —— 混着来的话
    "退款 -50" 和 "支出 50" 在求和时会互相抵消,而它们是两件事。
    """
    match = _AMOUNT.match(text or "")
    if match is None:
        return None
    try:
        value = Decimal(match.group("value").replace(",", ""))
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    direction = Direction.CREDIT if match.group("sign") == "-" else Direction.DEBIT
    return value, direction


def parse_kind(*fragments: str | None) -> TxnKind | None:
    """从"交易类型""交易描述"这类字段里认类型。**认不出就是 None。**

    收多个片段,是因为类型那一列常常是空的,而"银联还款"往往写在描述里。

    **顺序是有讲究的,不能按字典序排。** 真实存在的冲突有两处:

    - "转账还款"同时带着还款和转账 —— 还款在前,否则一笔还款会被记成转账,
      而转账不进支出统计,月度支出凭空少一块
    - "转入"不是收入 —— 转账排在收入前面,钱换了个地方不等于挣到了钱

    退款排在转账和收入前面是保险:它的字眼("退货""冲正")比别的都具体,
    而退款一定是钱回来。
    """
    haystack = " ".join(part for part in fragments if part)
    if any(marker in haystack for marker in _REPAYMENT_MARKERS):
        return TxnKind.REPAYMENT
    if any(marker in haystack for marker in _REFUND_MARKERS):
        return TxnKind.REFUND
    if any(marker in haystack for marker in _TRANSFER_MARKERS):
        # 转账排在收入前面:"转入"是钱换了个地方,不是挣到了钱
        return TxnKind.TRANSFER
    if any(marker in haystack for marker in _INCOME_MARKERS):
        return TxnKind.INCOME
    if any(marker in haystack for marker in _EXPENSE_MARKERS):
        return TxnKind.EXPENSE
    return None


def parse_date(text: str, *, year: int) -> date | None:
    """认日期。只有月日的补上 `year`。

    `year` 要由调用方给,**不从今天推**:一月收到的是去年十二月的账单,
    推出来的年份会让整份账单落到未来,而未来的交易不会出现在任何月度报表里。
    """
    stripped = (text or "").strip()
    cleaned = stripped.split()[0] if stripped else ""
    if not cleaned:
        return None
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return parsed.date().replace(year=year) if "%Y" not in fmt else parsed.date()
    return None


def card_tail(text: str) -> str | None:
    """从"招商银行储蓄卡(1234)"这种写法里取卡号后四位。

    四位数字后面允许跟非数字:**卡号常常写在括号里**,而只认"数字结尾"的话
    这种写法一个都取不到。表现不是报错,是卡号全空 —— 然后对账匹配少掉一道
    判据,3 天的窗口里只剩金额一个条件,认错两笔同额消费的机会大得多。
    """
    match = _CARD_TAIL.search((text or "").replace(" ", ""))
    return match.group(1) if match else None


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
