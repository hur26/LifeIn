"""`facts` 的读写 —— 记忆层里唯一"系统自己得出的结论"。

`raw_events` 是发生过的事,不会错;`facts` 是从那些事**推出来**的话,会错。
所以这一层的每条规则都在回答同一个问题:错了之后能不能查出来、能不能改掉。

**provenance 必填(铁律 5)。** 库里有 `cardinality(provenance) >= 1` 的 CHECK,
这里在进库前先拦一道。两道都要:CHECK 是最后一道防线,但它给不出人话的
错误信息,而这类错误多半发生在半夜的抽取任务里,报错信息就是唯一线索。

**external 来源封顶 0.6(06 §1.2 第 3 条)。** 邮件正文里写"我下周离职"可能是
转发的别人的话、可能是玩笑。系统自己读来的东西不许有高置信度,
**只有用户确认能突破这个上限** —— 和别名那条 1.0 是一个道理。

**被否定的事实不许再被推断回来。** 06 §2.3 说 `negated_by_user` 的记录保留不删,
用处就在这里:抽取是每天跑的,同一封邮件里同一句话明天还在。用户否定过一次
之后它又冒出来,比第一次冒出来更伤 —— 那说明"否定"这个动作没有用。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifein.models.normalized import Trust

log = logging.getLogger(__name__)

EXTERNAL_MAX_CONFIDENCE = 0.6
"""外部内容推出的事实,置信度上限。06 §1.2 第 3 条。"""

CONFIRMED_CONFIDENCE = 1.0

_WHITESPACE = re.compile(r"\s+")


class FactError(ValueError):
    """事实写不进去。**都在进库前抛**,带得出人话的原因。"""


def statement_key(statement: str) -> str:
    """去重用的键:去掉所有空白 + 小写。

    和 SQL 里的 `regexp_replace(lower(statement), '\\s+', '', 'g')` 必须一致,
    改一边等于改另一边。

    **刻意只做到这一步。** "张三下周三来北京"和"下周三张三来北京"是同一件事,
    这个键认不出来 —— 认出它需要语义比较,那是向量的活(第 5 片),
    不该在写入路径上做。这个键只负责挡住"同一句话被抽了两遍",
    而那恰恰是每天重跑抽取时最常发生的情况。
    """
    return _WHITESPACE.sub("", statement).lower()


def cap_confidence(confidence: float, trust: Trust) -> float:
    """按来源可信度封顶。纯函数,单独可测。"""
    if not 0.0 <= confidence <= 1.0:
        raise FactError(f"confidence 必须在 0 和 1 之间:{confidence}")
    if trust is Trust.EXTERNAL:
        return min(confidence, EXTERNAL_MAX_CONFIDENCE)
    return confidence


@dataclass(frozen=True)
class Fact:
    id: str
    statement: str
    provenance: list[int]
    confidence: float
    confirmed_by_user: bool
    negated_by_user: bool
    valid_from: datetime
    valid_until: datetime | None
    created_by_agent: str


@dataclass(frozen=True)
class AddResult:
    """写入的结果。`fact` 为 None 说明**故意没写**,`reason` 说明为什么。

    这里不抛异常:"用户否定过这条"不是错误,是系统按规矩没动作(铁律 7)。
    抽取任务一晚上遇上几十次是正常的,当异常处理会把日志淹掉。
    """

    fact: Fact | None
    created: bool
    reason: str = ""


_SELECT_BY_KEY = text("""
    SELECT id, statement, provenance, confidence, confirmed_by_user, negated_by_user,
           valid_from, valid_until, created_by_agent
      FROM facts
     WHERE user_id = :user_id
       AND regexp_replace(lower(statement), '\\s+', '', 'g') = :key
     ORDER BY negated_by_user DESC, created_at ASC
""")

_INSERT = text("""
    INSERT INTO facts
        (user_id, statement, provenance, confidence, valid_from, valid_until, created_by_agent)
    VALUES
        (:user_id, :statement, :provenance, :confidence, :valid_from, :valid_until, :agent)
    RETURNING id, statement, provenance, confidence, confirmed_by_user, negated_by_user,
              valid_from, valid_until, created_by_agent
""")

_MERGE_PROVENANCE = text("""
    UPDATE facts
       SET provenance = :provenance, confidence = :confidence
     WHERE user_id = :user_id AND id = :fact_id
 RETURNING id, statement, provenance, confidence, confirmed_by_user, negated_by_user,
           valid_from, valid_until, created_by_agent
""")

_SELECT_ONE = text("""
    SELECT id, statement, provenance, confidence, confirmed_by_user, negated_by_user,
           valid_from, valid_until, created_by_agent
      FROM facts
     WHERE user_id = :user_id AND id = :fact_id
""")

_NEGATE = text("""
    UPDATE facts SET negated_by_user = true
     WHERE user_id = :user_id AND id = :fact_id
""")

_CONFIRM = text("""
    UPDATE facts
       SET confirmed_by_user = true, confidence = :confidence, negated_by_user = false
     WHERE user_id = :user_id AND id = :fact_id
""")

_EXPIRE = text("""
    UPDATE facts SET valid_until = :valid_until
     WHERE user_id = :user_id AND id = :fact_id AND negated_by_user = false
""")

_LIST_ACTIVE = text("""
    SELECT id, statement, provenance, confidence, confirmed_by_user, negated_by_user,
           valid_from, valid_until, created_by_agent
      FROM facts
     WHERE user_id = :user_id
       AND negated_by_user = false
       AND confidence >= :min_confidence
       AND valid_from <= :at
       AND (valid_until IS NULL OR valid_until > :at)
     ORDER BY confirmed_by_user DESC, confidence DESC, valid_from DESC
     LIMIT :limit
""")

_SEARCH = text("""
    SELECT id, statement, provenance, confidence, confirmed_by_user, negated_by_user,
           valid_from, valid_until, created_by_agent
      FROM facts
     WHERE user_id = :user_id
       AND negated_by_user = false
       AND valid_from <= :at
       AND (valid_until IS NULL OR valid_until > :at)
       AND statement ILIKE :pattern
     ORDER BY confirmed_by_user DESC, confidence DESC, valid_from DESC
     LIMIT :limit
""")


def add_fact(
    user_id: str,
    session: Session,
    *,
    statement: str,
    provenance: Sequence[int],
    confidence: float,
    trust: Trust,
    created_by_agent: str,
    valid_from: datetime,
    valid_until: datetime | None = None,
) -> AddResult:
    """写入一条事实。

    `trust` 说的是**这条结论所依据的内容**可不可信,不是结论本身
    —— 和网关那个 `CallContext.trust` 同一个意思。

    三种不写的情况:
    - 用户否定过同一句话 → 不写,`reason=negated`
    - 已有同一句话且没被否定 → 不新建,把这次的 provenance 并进去
    - provenance 为空 → 抛 `FactError`,这是调用方的 bug,不是正常路径
    """
    if not statement.strip():
        raise FactError("statement 不能为空")
    if not provenance:
        # 库里的 CHECK 也会拦,但报错信息是一串英文约束名,半夜排查时没用
        raise FactError(f"没有来源的记忆不许落库(铁律 5):{statement[:40]}")
    if not created_by_agent.strip():
        raise FactError("created_by_agent 不能为空:出了问题要能定位到是哪个 agent 写的")

    capped = cap_confidence(confidence, trust)
    key = statement_key(statement)
    existing = session.execute(_SELECT_BY_KEY, {"user_id": user_id, "key": key}).all()

    for row in existing:
        if row.negated_by_user:
            log.info("用户否定过这条事实,不再写入:%s", statement[:60])
            return AddResult(fact=None, created=False, reason="negated")

    if existing:
        row = existing[0]
        merged = sorted({*(row.provenance or []), *provenance})
        # 又见到一次不会让外部来源变可信,所以照样封顶;用户确认过的不许被推断值压下去
        confidence_now = max(float(row.confidence), capped)
        updated = session.execute(
            _MERGE_PROVENANCE,
            {
                "user_id": user_id,
                "fact_id": row.id,
                "provenance": merged,
                "confidence": confidence_now,
            },
        ).one()
        return AddResult(fact=_to_fact(updated), created=False, reason="merged")

    row = session.execute(
        _INSERT,
        {
            "user_id": user_id,
            "statement": statement.strip(),
            "provenance": list(provenance),
            "confidence": capped,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "agent": created_by_agent,
        },
    ).one()
    return AddResult(fact=_to_fact(row), created=True)


def negate_fact(user_id: str, session: Session, *, fact_id: str) -> bool:
    """用户说这条不对。**只标记,不删** —— 删了明天就会被重新推断出来。"""
    return session.execute(_NEGATE, {"user_id": user_id, "fact_id": fact_id}).rowcount > 0


def confirm_fact(
    user_id: str,
    session: Session,
    *,
    fact_id: str,
    confidence: float = CONFIRMED_CONFIDENCE,
) -> bool:
    """用户确认这条对。**这是突破 0.6 上限的唯一路径。**

    顺带把 `negated_by_user` 清掉:先否定后又确认,说明是当初否错了。
    """
    if not 0.0 <= confidence <= 1.0:
        raise FactError(f"confidence 必须在 0 和 1 之间:{confidence}")
    result = session.execute(
        _CONFIRM, {"user_id": user_id, "fact_id": fact_id, "confidence": confidence}
    )
    return result.rowcount > 0


def expire_fact(user_id: str, session: Session, *, fact_id: str, valid_until: datetime) -> bool:
    """给事实划一个失效时间。"他在深圳"这类事实会过期,但过期不等于当初是错的。

    和 `negate_fact` 的区别要分清:否定说的是"这条从来就不对",
    失效说的是"这条曾经对"。混用会让 App 上那份"你否定过什么"的清单失去意义。
    """
    result = session.execute(
        _EXPIRE, {"user_id": user_id, "fact_id": fact_id, "valid_until": valid_until}
    )
    return result.rowcount > 0


def get_fact(user_id: str, session: Session, *, fact_id: str) -> Fact | None:
    row = session.execute(_SELECT_ONE, {"user_id": user_id, "fact_id": fact_id}).first()
    return _to_fact(row) if row else None


def list_active_facts(
    user_id: str,
    session: Session,
    *,
    at: datetime,
    min_confidence: float = 0.0,
    limit: int = 100,
) -> list[Fact]:
    """取在某个时刻仍然成立的事实。被否定的一条都不给。

    排序把用户确认过的排在前面:进 prompt 的条数有限,该先给最靠得住的。
    """
    rows = session.execute(
        _LIST_ACTIVE,
        {"user_id": user_id, "at": at, "min_confidence": min_confidence, "limit": limit},
    ).all()
    return [_to_fact(row) for row in rows]


def search_facts(
    user_id: str, session: Session, *, query: str, at: datetime, limit: int = 20
) -> list[Fact]:
    """按字面搜事实。语义召回是第 5 片的向量索引,这里只做字面。

    `at` 和 `list_active_facts` 是同一个意思,也没有默认值:两个入口对
    "现在成立的事实"要有同一个口径,否则搜出来的会包含已经过期的条目,
    而用户看不出那是过期的。
    """
    if not query.strip():
        return []
    rows = session.execute(
        _SEARCH, {"user_id": user_id, "pattern": f"%{query.strip()}%", "at": at, "limit": limit}
    ).all()
    return [_to_fact(row) for row in rows]


def _to_fact(row) -> Fact:
    return Fact(
        id=str(row.id),
        statement=row.statement,
        provenance=list(row.provenance or []),
        confidence=float(row.confidence),
        confirmed_by_user=row.confirmed_by_user,
        negated_by_user=row.negated_by_user,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        created_by_agent=row.created_by_agent,
    )
