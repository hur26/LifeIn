"""对账单 → `raw_events`。**第 6 片和第 7、8 片之间那一段。**

第 6 片(对账)读的是 `channel=statement` 的交易事件,第 7、8 片产出的是
`StatementRow`。中间这一步把后者变成前者 —— 没有它,那三片各自都能跑,
接起来却是空的:对账 job 每天查一次,永远查到零行。

## 一行一条,不是一封一条

06 §2.6 第三层写死了这一条:`UNIQUE (user_id, source_event_id)` 要求每笔交易
有自己的来源事件,**一封带 200 行的对账单共用一个 `source_event_id`
只能入账一笔**。

## external_id 决定了重导会不会变成两份账

`raw_events` 上的 `UNIQUE (user_id, source, external_id)` 是唯一挡住重复导入的
东西,所以这个 id 必须**同一行每次都算出同一个值**:

- **有订单号就用订单号。** 它是支付平台给的,天然唯一且稳定
- 没有的话(信用卡对账单常常没有)按内容算摘要:发卡行 + 日期 + 金额 +
  方向 + 商户。**同一份对账单里出现两行完全一样的**(便利店连买两次同价商品)
  再补一个序号 —— 少了它,真实的第二笔会被当成重复导入吞掉,
  而漏一笔在账本上是看不出来的

**不能用行号**:同一份账单换个解析路径(比如先按 PDF 后按导出)行号就变了,
于是同一笔进两次。
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, time, tzinfo

from lifein.models.normalized import (
    Amount,
    Direction,
    EventKind,
    ExternalRef,
    NormalizedEvent,
    Party,
    PartyRole,
    Trust,
)
from lifein.sources import statement
from lifein.sources.base import IngestedEvent
from lifein.sources.statement import StatementRow

log = logging.getLogger(__name__)

SOURCE = "statement"
"""`raw_events.source`。和 `raw.channel` 取同一个词不是巧合 ——
对账 job 按 `raw.channel` 找,而人排查时按 `source` 找,两边说的是同一件事。"""

POSTED_AT = time(12, 0)
"""对账单只给日期,不给时刻。**落在正午**,不是午夜。

落午夜的话,一笔 8 月 20 日的消费在东八区是 20 日 00:00,换算成 UTC 是
19 日 16:00 —— 跨过了日界,而对账匹配的 3 天窗口和月度报表都按天切。
正午前后各留 12 小时,时区怎么算都还在同一天。
"""


def to_events(
    rows: Sequence[StatementRow],
    *,
    issuer: str,
    tz: tzinfo,
    period: str | None = None,
) -> list[IngestedEvent]:
    """把解析出来的行变成能入库的事件。**一行一条。**

    `issuer` 是发卡行或平台("cmb""alipay""wechat")。它进 `external_id`,
    所以**同一笔交易从两个渠道来会是两条事件** —— 那是对的:
    对账那一步会把它们合起来,而在这里合会丢掉"两边各说了什么"。
    """
    events: list[IngestedEvent] = []
    seen: Counter[str] = Counter()

    for row in rows:
        key = _content_key(row, issuer=issuer)
        seen[key] += 1
        # 同一份账单里两行完全一样时,第二行带上序号。**少了它真实的第二笔
        # 会被当成重复导入吞掉**,而漏一笔在账本上看不出来
        external_id = key if seen[key] == 1 else f"{key}#{seen[key]}"

        occurred_at = datetime.combine(row.occurred_on, POSTED_AT, tzinfo=tz)
        events.append(
            IngestedEvent(
                source=SOURCE,
                external_id=external_id,
                occurred_at=occurred_at,
                trust=Trust.EXTERNAL,
                raw=_raw(row, issuer=issuer, period=period, occurred_at=occurred_at),
                normalized=_normalized(row, occurred_at=occurred_at, issuer=issuer),
            )
        )
    return events


def _normalized(row: StatementRow, *, occurred_at: datetime, issuer: str) -> NormalizedEvent:
    return NormalizedEvent(
        kind=EventKind.TRANSACTION,
        title=row.merchant_raw or issuer,
        occurred_at=occurred_at,
        external_ref=ExternalRef(source=SOURCE, external_id=_content_key(row, issuer=issuer)),
        trust=Trust.EXTERNAL,
        # 规则抠出来的,不是模型推的 —— 对账单是 ADR-012 那张表里的"金额权威源"
        confidence=1.0,
        amount=Amount(value=row.amount, currency="CNY", direction=row.direction),
        parties=(
            [Party(role=PartyRole.MERCHANT, display_name=row.merchant_raw)]
            if row.merchant_raw
            else []
        ),
    )


def _raw(
    row: StatementRow, *, issuer: str, period: str | None, occurred_at: datetime
) -> dict:
    """`raw` 的形状由 `sources/statement.py` 定,这里只是照着填。

    **不存整行原文。** [R10](../../docs/05-risks.md#r10--手机端采集器的越权读取)
    对交易类的要求是只留金额、时间、卡号后四位、商户 —— 对账单上还有余额、
    额度、积分,记账一样都用不上,而它们泄露出去的信息比一笔消费多得多。
    `raw_cells` 里的原始单元格**只在解析失败时有价值**,而这一行已经解成功了。
    """
    return {
        "channel": statement.CHANNEL,
        "statement": {"issuer": issuer, "period": period},
        "parsed": {
            "amount": str(row.amount),
            "currency": "CNY",
            "direction": row.direction.value,
            "account_hint": row.account_hint,
            "merchant_raw": row.merchant_raw,
            "order_no": row.order_no,
            "kind": row.kind.value if row.kind else None,
            "posted_at": occurred_at.isoformat(),
            "text_redacted": True,
        },
    }


def _content_key(row: StatementRow, *, issuer: str) -> str:
    """这一行的稳定身份。**同一行每次都要算出同一个值。**

    有订单号就用它:支付平台给的,天然唯一且稳定,而且换个解析路径也不会变。
    没有就按内容算摘要 —— 内容变了本来就该是另一笔。
    """
    if row.order_no:
        return f"stmt:{issuer}:{row.order_no}"

    digest = hashlib.sha256(
        "|".join(
            (
                issuer,
                row.occurred_on.isoformat(),
                str(row.amount),
                row.direction.value,
                row.merchant_raw or "",
                row.account_hint or "",
            )
        ).encode()
    ).hexdigest()[:16]
    return f"stmt:{issuer}:{digest}"


def period_of(rows: Sequence[StatementRow]) -> str | None:
    """这批行的账单周期(`YYYY-MM`)。**跨月就返回 None。**

    支付宝的导出可以自选区间,硬给一个月份会让"这是几月的账单"变成假信息,
    而那个字段将来是用来回答"八月的账单导过没有"的。
    """
    months = {(row.occurred_on.year, row.occurred_on.month) for row in rows}
    if len(months) != 1:
        return None
    year, month = next(iter(months))
    return f"{year:04d}-{month:02d}"


def summarize(events: Sequence[IngestedEvent]) -> dict[str, int]:
    """给调用方报数用。**支出和收入分开数** —— 一份账单里进账那几笔
    最容易出问题(退款和收入分不开),数字分开才看得出异常。"""
    counts = {"lines": len(events), "outbound": 0, "inbound": 0}
    for event in events:
        amount = event.normalized.amount if event.normalized else None
        if amount is None:
            continue
        if amount.direction is Direction.DEBIT:
            counts["outbound"] += 1
        else:
            counts["inbound"] += 1
    return counts
