"""`raw_events` 的读写。

事件流只追加不修改,是全系统的事实来源(架构 §2.1)。这一层要保证三件事:

**幂等。** 写入走 `ON CONFLICT (user_id, source, external_id) DO NOTHING`。
适配器被允许重复返回同一条事件(base.py 那条约定),重复由这里挡掉,
不由适配器记住上次取到哪 —— 后者一重启就失效。

**归一化失败照样入库。** `normalized` 留空、`normalize_error` 写原因,
`raw` 永远保留。解析器修好之后按同一个唯一键重跑,不会产生重复
(06 §1.4)。

**失败的能被数出来。** `count_failed` 服务于告警:数据源改格式时,
表现是摘要悄悄变短,不是报错(R8)。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifein.models.normalized import NormalizedEvent
from lifein.sources.base import IngestedEvent

log = logging.getLogger(__name__)

_INSERT = text("""
    INSERT INTO raw_events
        (user_id, source, external_id, occurred_at, trust, raw, normalized, normalize_error)
    VALUES
        (:user_id, :source, :external_id, :occurred_at, :trust,
         CAST(:raw AS JSONB), CAST(:normalized AS JSONB), :normalize_error)
    ON CONFLICT (user_id, source, external_id) DO NOTHING
    RETURNING id
""")

_SELECT_BETWEEN = text("""
    SELECT id, normalized
      FROM raw_events
     WHERE user_id = :user_id
       AND occurred_at >= :start
       AND occurred_at < :end
       AND normalized IS NOT NULL
     ORDER BY occurred_at DESC
     LIMIT :limit
""")

_COUNT_FAILED = text("""
    SELECT count(*)
      FROM raw_events
     WHERE user_id = :user_id
       AND ingested_at >= :since
       AND normalize_error IS NOT NULL
""")


@dataclass(frozen=True)
class InsertResult:
    inserted: int
    duplicates: int
    failed: int
    """其中归一化失败的条数。入库了,但要告警。"""


def insert_events(
    user_id: str,
    session: Session,
    events: Sequence[IngestedEvent],
) -> InsertResult:
    """写入一批事件。重复的静默跳过,失败的照样入库。"""
    inserted = duplicates = failed = 0

    for event in events:
        normalized_json = (
            event.normalized.model_dump_json() if event.normalized is not None else None
        )
        row = session.execute(
            _INSERT,
            {
                "user_id": user_id,
                "source": event.source,
                "external_id": event.external_id,
                "occurred_at": event.occurred_at,
                "trust": event.trust.value,
                "raw": _json(event.raw),
                "normalized": normalized_json,
                "normalize_error": event.normalize_error,
            },
        ).first()

        if row is None:
            duplicates += 1
            continue
        inserted += 1
        if event.failed:
            failed += 1

    if failed:
        # 不在这里发告警:仓储不该知道告警通道。数出来交给调用方
        log.warning("本批 %d 条事件归一化失败", failed)
    return InsertResult(inserted=inserted, duplicates=duplicates, failed=failed)


_REPARSE = text("""
    UPDATE raw_events
       SET normalized = CAST(:normalized AS JSONB),
           normalize_error = :normalize_error,
           occurred_at = :occurred_at
     WHERE user_id = :user_id
       AND source = :source
       AND external_id = :external_id
""")


def reparse_events(
    user_id: str,
    session: Session,
    events: Sequence[IngestedEvent],
) -> int:
    """用新的解析结果覆盖已有事件的 `normalized`。返回更新条数。

    06 §1.4 承诺"解析器修好后可以重跑",这就是那个重跑。
    **只改派生列,`raw` 一个字节都不动** —— 事件流"只追加不修改"说的是事实本身,
    而 `normalized` 是从事实算出来的,算法变了就该重算。

    注意邮件的 `raw` 里只有信头没有正文,所以重跑得先回 IMAP 把信重新拉一遍
    (`backfill --reparse` 就是这么做的)。信在服务器上删了就重跑不了了。
    """
    updated = 0
    for event in events:
        normalized_json = (
            event.normalized.model_dump_json() if event.normalized is not None else None
        )
        result = session.execute(
            _REPARSE,
            {
                "user_id": user_id,
                "source": event.source,
                "external_id": event.external_id,
                "occurred_at": event.occurred_at,
                "normalized": normalized_json,
                "normalize_error": event.normalize_error,
            },
        )
        updated += result.rowcount
    return updated


def fetch_normalized_between(
    user_id: str,
    session: Session,
    *,
    start: datetime,
    end: datetime,
    limit: int = 500,
) -> list[NormalizedEvent]:
    """取一个时间窗内已归一化的事件。摘要与问答都用它。

    时间窗按 `occurred_at`(事件真实发生时间)而不是 `ingested_at` ——
    一封昨天的邮件今天才收到,它属于昨天。
    """
    rows = session.execute(
        _SELECT_BETWEEN,
        {"user_id": user_id, "start": start, "end": end, "limit": limit},
    ).all()

    events: list[NormalizedEvent] = []
    for row in rows:
        try:
            events.append(NormalizedEvent.model_validate(row.normalized))
        except ValueError:
            # 库里存着当年合法、现在不合法的结构。跳过单条,不让一条坏数据
            # 毁掉整天的摘要;但要记下来,数量一多说明骨架改动没配迁移
            log.warning("raw_events.id=%s 的 normalized 结构已不合法,跳过", row.id)
    return events


def count_failed(user_id: str, session: Session, *, since: datetime) -> int:
    """统计归一化失败数,供告警使用。

    数据源改格式时表现是摘要悄悄变短,不是报错(R8)—— 这个数字是唯一
    能提前发现它的地方。
    """
    return int(session.execute(_COUNT_FAILED, {"user_id": user_id, "since": since}).scalar_one())


def _json(payload: object) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)
