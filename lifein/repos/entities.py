"""`entities` 与 `entity_aliases` 的读写 —— 记忆层的第一块。

**别名归并整条链路不碰 LLM。** 铁律 9:能用规则拿到的字段不许交给 LLM。
邮箱、手机号、企微 userid、商户号都是精确标识符,字符串相等就是同一个人,
让模型去"判断这两个是不是一个人"只会引入一类无法复现的错误 —— 而记忆一旦
串了人,后面所有回答都跟着错,还查不出是哪一步错的。

模型在记忆层只做一件事:从正文里**读出**名字和关系(第 3 片的抽取 agent)。
读出来之后归到哪个实体,由这里的规则决定。

三条设计上的硬选择:

**别名列存归一化后的键,不存原样。** UNIQUE 建在 `(user_id, alias, alias_type)`
上,不归一化就会有 `Zhang@QQ.com` 和 `zhang@qq.com` 两行指向两个实体。
给人看的那个名字在 `entities.canonical_name` 里,那一列保留原样。

**冲突时不动作**(铁律 7)。邮箱指向实体 A、名字指向实体 B,说明两个实体
可能其实是一个人 —— 但合并实体不可逆,而证据只有"某封邮件里同时出现过"
的时候,合错了比不合糟得多。这里返回 `conflicted=True` 交给上层记下来,
不自动改任何一行。

**系统推断的置信度封顶 0.9,1.0 只能由用户确认。** 留出这一档不是洁癖:
App 上要能区分"它自己猜的"和"你点过确认的",不留档就区分不了。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifein.models.normalized import IdentifierType

log = logging.getLogger(__name__)


class EntityKind(StrEnum):
    """实体类型,取值同 06 §2.2。

    库里这一列没有 CHECK 约束,靠这个枚举挡。和 `facts.provenance` 那条不同:
    kind 写错只是多一行没人用的数据,不构成安全问题,不值得为它加一次迁移。
    """

    PERSON = "person"
    MERCHANT = "merchant"
    PLACE = "place"
    PROJECT = "project"
    SUBSCRIPTION = "subscription"
    RECURRING = "recurring"


class AliasType(StrEnum):
    NAME = "name"
    EMAIL = "email"
    PHONE = "phone"
    WECOM_USERID = "wecom_userid"
    MERCHANT_CODE = "merchant_code"


STRONG_TYPES = frozenset(
    {AliasType.EMAIL, AliasType.PHONE, AliasType.WECOM_USERID, AliasType.MERCHANT_CODE}
)
"""精确标识符。一次出现就够,不需要攒证据。

`name` 不在里面 —— 重名是常态,"张伟"在通讯录里可能有三个。
"""

INITIAL_STRONG = 0.9
INITIAL_NAME = 0.5
"""06 §2.2 的默认值:新别名先低置信度写入,累积证据后提升。"""

EVIDENCE_STEP = 0.1
MAX_INFERRED = 0.9
CONFIRMED = 1.0

MAX_EVIDENCE = 20
"""证据 id 最多留 20 条,**留最早的那些**。

一个每天发邮件的同事一年能攒 365 条,而这一列的用处是回答"系统凭什么认为
这两个名字是一个人" —— 回答它靠的是最早那几条,不是上周那几条。
置信度只用到条数,不用到内容,所以截断不影响判断。
"""

_PHONE_JUNK = re.compile(r"[\s\-()]+")
_WHITESPACE = re.compile(r"\s+")


def alias_type_for_identifier(identifier_type: IdentifierType) -> AliasType | None:
    """把归一化骨架里的 `identifier_type` 映射成别名类型。

    `card_last4` 映射不过来,返回 None:**卡号后四位标识的是一张卡,不是一个人。**
    拿它当别名会把两个碰巧同尾号的账户归并成同一个实体。卡与人的关系是账务
    自己的事,P2 的 `transactions` 里处理。
    """
    match identifier_type:
        case IdentifierType.EMAIL:
            return AliasType.EMAIL
        case IdentifierType.PHONE:
            return AliasType.PHONE
        case IdentifierType.WECOM_USERID:
            return AliasType.WECOM_USERID
        case IdentifierType.MERCHANT_CODE:
            return AliasType.MERCHANT_CODE
        case IdentifierType.CARD_LAST4:
            return None
    return None


def normalize_alias(alias: str, alias_type: AliasType) -> str:
    """算出别名的存储键。归一化后为空返回空串,由调用方拒绝。

    各类型的口径:

    - `email` / `wecom_userid` / `merchant_code`:去空白 + 小写。
      邮箱本地部分理论上区分大小写,实践中没有一家主流邮箱这么做,
      而按大小写分成两个实体的代价是记忆里凭空多出一个人
    - `phone`:去掉空格、横杠、括号,`+86` 前缀去掉。同一个号码在邮件签名里
      写作 `+86 138-0000-0000`、在通讯录里写作 `13800000000`,是同一个人
    - `name`:去首尾空白、内部连续空白压成一个空格,**大小写保留**。
      中文名没有大小写,英文名压成小写会让 `canonical_name` 之外再无原样可查
    """
    value = alias.strip()
    if not value:
        return ""

    if alias_type is AliasType.PHONE:
        cleaned = _PHONE_JUNK.sub("", value)
        if cleaned.startswith("+86"):
            cleaned = cleaned[3:]
        return cleaned
    if alias_type is AliasType.NAME:
        return _WHITESPACE.sub(" ", value)
    return _WHITESPACE.sub("", value).lower()


def initial_confidence(alias_type: AliasType) -> float:
    return INITIAL_STRONG if alias_type in STRONG_TYPES else INITIAL_NAME


def promote_confidence(alias_type: AliasType, evidence_count: int) -> float:
    """按证据条数算置信度。**纯函数,不碰库** —— 这条规则要能被单独测。

    精确标识符不随证据涨:它一开始就是 0.9,再来一百封邮件也不会更精确。
    名字每多一条独立证据涨 0.1,封顶 0.9。
    """
    if alias_type in STRONG_TYPES:
        return INITIAL_STRONG
    return min(MAX_INFERRED, INITIAL_NAME + EVIDENCE_STEP * max(0, evidence_count - 1))


class AliasError(ValueError):
    """别名本身不合法。归一化之后为空的别名不许落库。"""


@dataclass(frozen=True)
class Entity:
    id: str
    kind: EntityKind
    canonical_name: str
    attributes: dict
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class AliasLink:
    entity_id: str
    confidence: float
    evidence_event_ids: list[int]
    created: bool
    conflicted: bool
    """真:这个别名已经指向**别的**实体,本次一行没改。"""


@dataclass(frozen=True)
class Resolution:
    """一次归并的结果。

    `conflicted` 为真时 `entity` 是**精确标识符指向的那个** —— 标识符永远
    赢过名字。另一个实体一行没改,等人来看。
    """

    entity: Entity
    created: bool
    conflicted: bool = False


_SELECT_BY_ALIAS = text("""
    SELECT e.id, e.kind, e.canonical_name, e.attributes, e.first_seen_at, e.last_seen_at
      FROM entity_aliases a
      JOIN entities e ON e.id = a.entity_id AND e.user_id = a.user_id
     WHERE a.user_id = :user_id AND a.alias = :alias AND a.alias_type = :alias_type
""")

_SELECT_ENTITY = text("""
    SELECT id, kind, canonical_name, attributes, first_seen_at, last_seen_at
      FROM entities
     WHERE user_id = :user_id AND id = :entity_id
""")

_INSERT_ENTITY = text("""
    INSERT INTO entities (user_id, kind, canonical_name, attributes, first_seen_at, last_seen_at)
    VALUES (:user_id, :kind, :canonical_name, CAST(:attributes AS JSONB), :seen_at, :seen_at)
    RETURNING id, kind, canonical_name, attributes, first_seen_at, last_seen_at
""")

_TOUCH_ENTITY = text("""
    UPDATE entities
       SET first_seen_at = LEAST(first_seen_at, :seen_at),
           last_seen_at  = GREATEST(last_seen_at, :seen_at)
     WHERE user_id = :user_id AND id = :entity_id
 RETURNING id, kind, canonical_name, attributes, first_seen_at, last_seen_at
""")

_SELECT_ALIASES_OF_TYPE = text("""
    SELECT alias
      FROM entity_aliases
     WHERE user_id = :user_id AND entity_id = :entity_id AND alias_type = :alias_type
""")

_SELECT_ALIAS_ROW = text("""
    SELECT entity_id, confidence, evidence_event_ids
      FROM entity_aliases
     WHERE user_id = :user_id AND alias = :alias AND alias_type = :alias_type
""")

_INSERT_ALIAS = text("""
    INSERT INTO entity_aliases
        (user_id, entity_id, alias, alias_type, confidence, evidence_event_ids)
    VALUES (:user_id, :entity_id, :alias, :alias_type, :confidence, :evidence)
    ON CONFLICT (user_id, alias, alias_type) DO NOTHING
    RETURNING id
""")

_UPDATE_ALIAS = text("""
    UPDATE entity_aliases
       SET confidence = :confidence, evidence_event_ids = :evidence
     WHERE user_id = :user_id AND alias = :alias AND alias_type = :alias_type
""")

_CONFIRM_ALIAS = text("""
    UPDATE entity_aliases
       SET confidence = :confidence
     WHERE user_id = :user_id AND alias = :alias AND alias_type = :alias_type
""")

_SEARCH_BY_NAME = text("""
    SELECT e.id, e.kind, e.canonical_name, e.attributes,
           e.first_seen_at, e.last_seen_at
      FROM entities e
     WHERE e.user_id = :user_id
       AND (e.canonical_name ILIKE :pattern
            OR EXISTS (SELECT 1
                         FROM entity_aliases a
                        WHERE a.user_id = e.user_id
                          AND a.entity_id = e.id
                          AND a.alias ILIKE :pattern))
     ORDER BY e.last_seen_at DESC
     LIMIT :limit
""")


def link_alias(
    user_id: str,
    session: Session,
    *,
    entity_id: str,
    alias: str,
    alias_type: AliasType,
    evidence_event_id: int | None = None,
) -> AliasLink:
    """把一个别名挂到实体上,已存在就累积证据并按规则提升置信度。

    证据去重按 `raw_events.id`:同一条事件被重跑两次不该让置信度涨两次
    (归一化重跑是常规操作,见 06 §1.4)。
    """
    key = normalize_alias(alias, alias_type)
    if not key:
        raise AliasError(f"别名归一化后为空:{alias!r}({alias_type})")

    params = {"user_id": user_id, "alias": key, "alias_type": alias_type.value}
    existing = session.execute(_SELECT_ALIAS_ROW, params).first()

    if existing is None:
        evidence = [evidence_event_id] if evidence_event_id is not None else []
        confidence = initial_confidence(alias_type)
        row = session.execute(
            _INSERT_ALIAS,
            {**params, "entity_id": entity_id, "confidence": confidence, "evidence": evidence},
        ).first()
        if row is not None:
            return AliasLink(entity_id, confidence, evidence, created=True, conflicted=False)
        # 并发下有人抢先插了同一个键。重读一遍走下面的累积分支,不重试插入
        existing = session.execute(_SELECT_ALIAS_ROW, params).one()

    if str(existing.entity_id) != str(entity_id):
        # 铁律 7:拿不准不动作。合并实体不可逆,证据不够时合错比不合糟
        log.info(
            "别名 %s(%s)已指向实体 %s,本次要挂到 %s —— 不动作,记冲突",
            key,
            alias_type,
            existing.entity_id,
            entity_id,
        )
        return AliasLink(
            str(existing.entity_id),
            float(existing.confidence),
            list(existing.evidence_event_ids or []),
            created=False,
            conflicted=True,
        )

    evidence = list(existing.evidence_event_ids or [])
    if evidence_event_id is not None and evidence_event_id not in evidence:
        if len(evidence) < MAX_EVIDENCE:
            evidence.append(evidence_event_id)
        else:
            # 满了只涨计数不留 id 是自欺欺人:置信度会靠一个查不到出处的
            # 数字往上走。何况到这里 0.9 早就封顶了
            log.debug("别名 %s 的证据已满 %d 条,不再追加", key, MAX_EVIDENCE)

    confidence = max(float(existing.confidence), promote_confidence(alias_type, len(evidence)))
    session.execute(_UPDATE_ALIAS, {**params, "confidence": confidence, "evidence": evidence})
    return AliasLink(entity_id, confidence, evidence, created=False, conflicted=False)


def confirm_alias(user_id: str, session: Session, *, alias: str, alias_type: AliasType) -> bool:
    """用户确认这个别名确实指向这个实体。提到 1.0 —— 只有这条路径能到 1.0。"""
    key = normalize_alias(alias, alias_type)
    if not key:
        raise AliasError(f"别名归一化后为空:{alias!r}({alias_type})")
    result = session.execute(
        _CONFIRM_ALIAS,
        {"user_id": user_id, "alias": key, "alias_type": alias_type.value, "confidence": CONFIRMED},
    )
    return result.rowcount > 0


def find_by_alias(
    user_id: str, session: Session, *, alias: str, alias_type: AliasType
) -> Entity | None:
    """按别名找实体。找不到返回 None,不猜。"""
    key = normalize_alias(alias, alias_type)
    if not key:
        return None
    row = session.execute(
        _SELECT_BY_ALIAS,
        {"user_id": user_id, "alias": key, "alias_type": alias_type.value},
    ).first()
    return _to_entity(row) if row else None


def get_entity(user_id: str, session: Session, *, entity_id: str) -> Entity | None:
    row = session.execute(_SELECT_ENTITY, {"user_id": user_id, "entity_id": entity_id}).first()
    return _to_entity(row) if row else None


def search_entities(user_id: str, session: Session, *, name: str, limit: int = 10) -> list[Entity]:
    """按名字模糊找实体,给问答用("上次和 X 聊的是什么")。

    模糊只到 `ILIKE %x%` 为止,不做拼音、不做编辑距离:这一层回答的是
    "你打的名字对不对得上",对不上就该让用户换个说法,而不是给他一个像的人。
    真正的模糊召回是向量的事(第 5 片)。
    """
    pattern = f"%{normalize_alias(name, AliasType.NAME)}%"
    rows = session.execute(
        _SEARCH_BY_NAME, {"user_id": user_id, "pattern": pattern, "limit": limit}
    ).all()
    return [_to_entity(row) for row in rows]


def resolve_or_create(
    user_id: str,
    session: Session,
    *,
    kind: EntityKind,
    name: str,
    seen_at: datetime,
    identifier: str | None = None,
    identifier_type: AliasType | None = None,
    evidence_event_id: int | None = None,
) -> Resolution:
    """归并入口:给一个名字(可能还带一个精确标识符),拿到实体。

    顺序是**先精确标识符,后名字** —— 邮箱对上了就是他,哪怕这次署名换了。
    反过来会把两个都叫"李工"的人归成一个。

    两边都对上、对上的却不是同一个实体 → `conflicted=True`,以标识符那个为准,
    名字那条别名一个字节都不改。

    **标识符没见过、名字却对上了** 是最难的一种,分两种情况(见 `_is_someone_else`)。
    """
    if identifier is not None and identifier_type is None:
        raise AliasError("给了 identifier 就必须给 identifier_type")
    if not name.strip():
        raise AliasError("name 不能为空:实体总得有个能显示的名字")

    by_identifier = (
        find_by_alias(user_id, session, alias=identifier, alias_type=identifier_type)
        if identifier and identifier_type
        else None
    )
    by_name = find_by_alias(user_id, session, alias=name, alias_type=AliasType.NAME)

    conflicted = (
        by_identifier is not None and by_name is not None and by_identifier.id != by_name.id
    )

    if (
        by_identifier is None
        and by_name is not None
        and identifier
        and identifier_type
        and _is_someone_else(
            user_id,
            session,
            entity_id=by_name.id,
            alias=identifier,
            alias_type=identifier_type,
        )
    ):
        # 同名,但那个人的邮箱我们已经知道且不是这个 —— 是另一个人
        conflicted = True
        by_name = None

    found = by_identifier or by_name
    created = found is None
    if found is None:
        entity = _to_entity(
            session.execute(
                _INSERT_ENTITY,
                {
                    "user_id": user_id,
                    "kind": kind.value,
                    "canonical_name": name.strip(),
                    "attributes": "{}",
                    "seen_at": seen_at,
                },
            ).one()
        )
    else:
        entity = _to_entity(
            session.execute(
                _TOUCH_ENTITY,
                {"user_id": user_id, "entity_id": found.id, "seen_at": seen_at},
            ).one()
        )

    if identifier and identifier_type:
        link = link_alias(
            user_id,
            session,
            entity_id=entity.id,
            alias=identifier,
            alias_type=identifier_type,
            evidence_event_id=evidence_event_id,
        )
        conflicted = conflicted or link.conflicted

    if not conflicted:
        # 冲突时连名字别名也不动:那一行正是冲突的一半,改了就毁掉现场
        link = link_alias(
            user_id,
            session,
            entity_id=entity.id,
            alias=name,
            alias_type=AliasType.NAME,
            evidence_event_id=evidence_event_id,
        )
        conflicted = link.conflicted

    return Resolution(entity=entity, created=created, conflicted=conflicted)


def _is_someone_else(
    user_id: str,
    session: Session,
    *,
    entity_id: str,
    alias: str,
    alias_type: AliasType,
) -> bool:
    """名字对上了、标识符没见过 —— 判断这是不是**另一个同名的人**。

    判据是那个实体身上有没有同类型的别的标识符:

    - **没有** → 它到目前为止只是个名字,这次多知道了一个邮箱,挂上去
    - **有,且不是这个** → 我们已经知道"李工"的邮箱,现在来了个邮箱不同的"李工"。
      按同一个人处理就把两个人的记忆混进一份了

    第二种情况会给一个人建出重复实体(比如他换了个工作邮箱发信)。
    这是刻意选的失败方向:**重复实体是看得见、能合并的;归错的人是看不见的。**
    用户在 App 上纠正一次就好,而混进去的记忆没人会想到去查。
    """
    key = normalize_alias(alias, alias_type)
    rows = session.execute(
        _SELECT_ALIASES_OF_TYPE,
        {"user_id": user_id, "entity_id": entity_id, "alias_type": alias_type.value},
    ).all()
    return any(row.alias != key for row in rows)


def _to_entity(row) -> Entity:
    return Entity(
        id=str(row.id),
        kind=EntityKind(row.kind),
        canonical_name=row.canonical_name,
        attributes=dict(row.attributes or {}),
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
    )
