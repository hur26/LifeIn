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

_SELECT_TRANSACTIONS_BETWEEN = text("""
    SELECT id, normalized, raw
      FROM raw_events
     WHERE user_id = :user_id
       AND occurred_at >= :start
       AND occurred_at < :end
       AND normalized IS NOT NULL
       AND normalized ->> 'kind' = 'transaction'
     ORDER BY occurred_at DESC
     LIMIT :limit
""")

_SELECT_UNRECONCILED_STATEMENT_LINES = text("""
    SELECT e.id, e.normalized, e.raw
      FROM raw_events e
     WHERE e.user_id = :user_id
       AND e.normalized IS NOT NULL
       AND e.normalized ->> 'kind' = 'transaction'
       AND e.raw ->> 'channel' = :channel
       -- 还没被处理过的：既没自己变成一笔交易，
       -- 也没被回填到别人身上。这两种就是对账的全部结局
       AND NOT EXISTS (
           SELECT 1 FROM transactions t
            WHERE t.user_id = e.user_id
              AND (t.source_event_id = e.id OR t.matched_statement_event_id = e.id)
       )
     ORDER BY e.occurred_at
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


@dataclass(frozen=True)
class StoredEvent:
    """一条已归一化的事件,连同它在 `raw_events` 里的 id。

    记忆层非要这个 id 不可:`facts.provenance` 存的就是它(铁律 5)。
    摘要不需要,所以上面那个 `fetch_normalized_between` 保持原样 ——
    它是 P0 验收期间正在跑的链路,这一期一个字节都不动。
    """

    event_id: int
    event: NormalizedEvent
    raw: dict | None = None
    """采集时留下的原始载荷。**只有交易那条链路会用到**,别的取法不填。

    记账的第 4 层复核要拿 `raw["parsed"]["matched"]`(规则抠金额时匹配到的
    那一小段原文)做逐字比对。不带它的话那一层在生产里是空转的:
    交易正文按 R10 脱敏之后 `event.body` 是空的,比对无从下手,
    而"没法比对"和"比对通过"在代码里长得一模一样。
    """


def fetch_stored_between(
    user_id: str,
    session: Session,
    *,
    start: datetime,
    end: datetime,
    limit: int = 500,
) -> list[StoredEvent]:
    """取一个时间窗内已归一化的事件,**带上 id**。记忆抽取用它。"""
    rows = session.execute(
        _SELECT_BETWEEN,
        {"user_id": user_id, "start": start, "end": end, "limit": limit},
    ).all()

    stored: list[StoredEvent] = []
    for row in rows:
        try:
            stored.append(
                StoredEvent(event_id=row.id, event=NormalizedEvent.model_validate(row.normalized))
            )
        except ValueError:
            log.warning("raw_events.id=%s 的 normalized 结构已不合法,跳过", row.id)
    return stored



def fetch_transactions_between(
    user_id: str,
    session: Session,
    *,
    start: datetime,
    end: datetime,
    limit: int = 500,
) -> list[StoredEvent]:
    """取一个时间窗内的**交易事件**,连原始载荷一起。记账 job 用它。

    在 SQL 里就按 `kind = 'transaction'` 筛掉,而不是取回来再过滤:
    一天的事件里交易只占很小一部分,把群消息也捞出来只是白白解析一遍 JSON。
    """
    rows = session.execute(
        _SELECT_TRANSACTIONS_BETWEEN,
        {"user_id": user_id, "start": start, "end": end, "limit": limit},
    ).all()

    stored: list[StoredEvent] = []
    for row in rows:
        try:
            stored.append(
                StoredEvent(
                    event_id=row.id,
                    event=NormalizedEvent.model_validate(row.normalized),
                    raw=row.raw if isinstance(row.raw, dict) else None,
                )
            )
        except ValueError:
            log.warning("raw_events.id=%s 的 normalized 结构已不合法,跳过", row.id)
    return stored



def fetch_unreconciled_statement_lines(
    user_id: str,
    session: Session,
    *,
    channel: str = "statement",
    limit: int = 1000,
) -> list[StoredEvent]:
    """取**还没对过账的对账单行**。对账 job 用它。

    它不按时间窗取，而是按“处理过没有”取 —— 这是故意的：
    一行对账单的 `occurred_at` 是**那笔消费当时的日期**（上个月），
    而它落地是本月的事。按时间窗取的 job 永远看不到它们。

    “处理过”只有两种形态：自己变成了一笔交易（`source_event_id`），
    或者被回填到了实时那一笔上（`matched_statement_event_id`）。
    **这两道同时也是幂等键**，所以重跑多少次都是同一个结果。
    """
    rows = session.execute(
        _SELECT_UNRECONCILED_STATEMENT_LINES,
        {"user_id": user_id, "channel": channel, "limit": limit},
    ).all()

    stored: list[StoredEvent] = []
    for row in rows:
        try:
            stored.append(
                StoredEvent(
                    event_id=row.id,
                    event=NormalizedEvent.model_validate(row.normalized),
                    raw=row.raw if isinstance(row.raw, dict) else None,
                )
            )
        except ValueError:
            log.warning("raw_events.id=%s 的 normalized 结构已不合法，跳过", row.id)
    return stored


_SELECT_BY_PARTY = text("""
    SELECT id, normalized
      FROM raw_events
     WHERE user_id = :user_id
       AND normalized IS NOT NULL
       AND EXISTS (
           SELECT 1
             FROM jsonb_array_elements(normalized -> 'parties') AS p
            WHERE lower(p ->> 'identifier') = ANY(:identifiers)
               OR lower(p ->> 'display_name') = ANY(:names)
       )
     ORDER BY occurred_at DESC
     LIMIT :limit
""")


def fetch_events_with_party(
    user_id: str,
    session: Session,
    *,
    identifiers: Sequence[str],
    names: Sequence[str],
    limit: int = 20,
) -> list[StoredEvent]:
    """找参与方里有这些标识符或名字的事件,最近的在前。

    "上次和张三聊的是什么"最终落到的就是这个查询。**入参是别名表给的**,
    不是用户随口打的那个词 —— 中间隔着实体归并那一层,所以他换过的邮箱、
    署过的别名都能命中。

    比对前两边都转小写:事件里存的是当初那封邮件写的原样(`Zhang@QQ.com`),
    别名表里存的是归一化后的键(`zhang@qq.com`)。

    走的是 `jsonb_array_elements` 而不是 `@>` 包含运算,所以用不上
    `normalized` 上那个 GIN 索引。**这是明知故犯**:一个人一年的事件量在
    万条这个数量级,顺序扫几十毫秒;而为了用上索引要把"或"拆成多次包含查询,
    换来的复杂度现在不值。真慢了再改,那时的判据是 EXPLAIN 而不是猜。
    """
    if not identifiers and not names:
        # 一个都没有就不查:空数组给到 ANY 会匹配不到任何行,但那是巧合不是设计
        return []

    rows = session.execute(
        _SELECT_BY_PARTY,
        {
            "user_id": user_id,
            "identifiers": [i.lower() for i in identifiers],
            "names": [n.lower() for n in names],
            "limit": limit,
        },
    ).all()

    stored: list[StoredEvent] = []
    for row in rows:
        try:
            stored.append(
                StoredEvent(event_id=row.id, event=NormalizedEvent.model_validate(row.normalized))
            )
        except ValueError:
            log.warning("raw_events.id=%s 的 normalized 结构已不合法,跳过", row.id)
    return stored


@dataclass(frozen=True)
class EventRef:
    """一条事件的"身份证":够用来说清"这条记忆是哪来的",不含正文。

    App 上那份记忆列表要在每条事实旁边显示出处(06 §6.10)——
    P1 的退出条件写着"记忆里出现说不清来源的条目就说明 provenance 链路有漏",
    而让来源和事实并排显示,是那句话唯一能被日常验证的形式。

    **只给标题不给正文**:正文可能是别人在群里说的话,而查一条记忆的出处
    不需要把那段话再读一遍(R10)。通知过了保留期正文会被清掉,标题还在,
    所以这条链路在清理之后照样成立。
    """

    event_id: int
    source: str
    title: str | None
    occurred_at: datetime


_SELECT_REFS = text("""
    SELECT id, source, occurred_at, normalized ->> 'title' AS title
      FROM raw_events
     WHERE user_id = :user_id
       AND id = ANY(:event_ids)
     ORDER BY occurred_at DESC
""")


def fetch_refs(user_id: str, session: Session, *, event_ids: Sequence[int]) -> list[EventRef]:
    """按 id 批量取事件的来源与标题。记忆浏览拿它显示"这条哪来的"。"""
    if not event_ids:
        return []
    rows = session.execute(
        _SELECT_REFS, {"user_id": user_id, "event_ids": list({int(i) for i in event_ids})}
    ).all()
    return [
        EventRef(
            event_id=row.id,
            source=row.source,
            title=row.title,
            occurred_at=row.occurred_at,
        )
        for row in rows
    ]


def count_failed(user_id: str, session: Session, *, since: datetime) -> int:
    """统计归一化失败数,供告警使用。

    数据源改格式时表现是摘要悄悄变短,不是报错(R8)—— 这个数字是唯一
    能提前发现它的地方。
    """
    return int(session.execute(_COUNT_FAILED, {"user_id": user_id, "since": since}).scalar_one())


def _json(payload: object) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)
