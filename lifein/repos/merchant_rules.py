"""商户归类规则表(06 的 `merchant_rules`)——
[ADR-008](../../docs/04-tech-decisions.md#adr-008--账单归类用规则llm-混合) 的落地。

两级策略:**先查规则表,命中就直接归类;没命中才用模型的答案,并把它写回规则表。**
消费记录高度重复,几十个商户占掉绝大多数流水,所以这张表会越来越管用 ——
ADR-008 给的监控指标是 **LLM 归类占比**,而且写明了:

> 如果这个数字不随时间下降,说明规则没有正确沉淀,是 bug 而不是常态。

`llm_share()` 就是那个数字。

## 这个模块最要紧的一条

**规则只从回填后的真实商户名沉淀。** 实时通知里的商户名常常是代收机构
("财付通""支付宝"),拿它建规则的话,表里会出现一条
`财付通 → 餐饮`,然后你在便利店、加油站、药店的每一笔都被归成餐饮 ——
一条规则污染掉后面所有的流水,而且**越用越像是对的**(hit_count 一直涨)。

所以 `remember()` 对两种输入直接拒绝:代收机构名,和枚举外的分类。
拒绝是静默的(返回 None),因为这不是错误,是常态 ——
实时那一遍本来就大多数抠不出真商户。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifein.repos.transactions import CATEGORIES

log = logging.getLogger(__name__)

_EDGE_PUNCT = re.compile(r"^[-—·:：,，。.]+|[-—·:：,，。.]+$")
"""首尾的标点。银行短信里商户名前后常挂着分隔符,带着它建规则会查不中。"""


class MatchType(StrEnum):
    EXACT = "exact"
    PREFIX = "prefix"
    REGEX = "regex"


class CreatedBy(StrEnum):
    LLM = "llm"
    """模型归的类,沉淀下来的。"""

    USER = "user"
    """用户手改的。**优先级高于 llm** —— 用户改过一次的东西,
    不该被下个月的模型再改回去。"""


PAYMENT_INTERMEDIARIES = frozenset(
    {
        "财付通",
        "支付宝",
        "微信支付",
        "微信",
        "银联",
        "云闪付",
        "网联",
        "快捷支付",
        "京东支付",
        "美团支付",
        "度小满",
        "银联商务",
        "拉卡拉",
        "通联支付",
        "汇付天下",
        "支付通",
        "第三方支付",
    }
)
"""**这些名字不许进规则表。** 它们是收钱的通道,不是你消费的地方。

判定用包含而不是相等:银行短信里写的是"财付通-某某商户"
"支付宝(中国)网络技术有限公司"这种。宁可多挡几个 ——
挡错了只是这一笔少一条规则,放错了是往后每一笔都归错类。
"""

MIN_MERCHANT_LENGTH = 2
"""一个字的"商"、"店"不足以成为规则:它会前缀匹配上一大片东西。"""


@dataclass(frozen=True)
class MerchantRule:
    id: int
    pattern: str
    match_type: MatchType
    category: str
    created_by: CreatedBy
    hit_count: int


@dataclass(frozen=True)
class Categorized:
    """一次归类的结果。**`by_rule` 是那个监控指标的原料。**"""

    category: str | None
    by_rule: bool
    rule_id: int | None = None


_LOOKUP = text("""
    SELECT id, pattern, match_type, category, created_by, hit_count
      FROM merchant_rules
     WHERE user_id = :user_id
       AND (
            (match_type = 'exact'  AND pattern = :merchant)
         OR (match_type = 'prefix' AND :merchant LIKE pattern || '%')
         OR (match_type = 'regex'  AND :merchant ~ pattern)
       )
     -- 用户改过的排在模型归的前面;同级里精确胜过前缀、前缀胜过正则,
     -- 再同级取更长的 pattern —— 更长意味着更具体
     ORDER BY (created_by = 'user') DESC,
              array_position(ARRAY['exact','prefix','regex'], match_type),
              length(pattern) DESC
     LIMIT 1
""")

_BUMP = text("""
    UPDATE merchant_rules
       SET hit_count = hit_count + 1, last_hit_at = now()
     WHERE user_id = :user_id AND id = :id
""")

_UPSERT = text("""
    INSERT INTO merchant_rules (user_id, pattern, match_type, category, created_by)
    VALUES (:user_id, :pattern, :match_type, :category, :created_by)
    ON CONFLICT (user_id, pattern, match_type)
    -- 已经有规则时:用户的覆盖模型的,模型的不覆盖用户的。
    -- 少了这个 WHERE,用户手改的分类会在下个月被模型悄悄改回去
    DO UPDATE SET category = EXCLUDED.category, created_by = EXCLUDED.created_by
              WHERE merchant_rules.created_by = 'llm'
                 OR EXCLUDED.created_by = 'user'
    RETURNING id, pattern, match_type, category, created_by, hit_count
""")

_LIST = text("""
    SELECT id, pattern, match_type, category, created_by, hit_count
      FROM merchant_rules
     WHERE user_id = :user_id
     ORDER BY hit_count DESC, id
     LIMIT :limit
""")

_DELETE = text("DELETE FROM merchant_rules WHERE user_id = :user_id AND id = :id")


def normalize(merchant: str | None) -> str | None:
    """归一化商户名。**查和写都要走它**,否则"星巴克 "和"星巴克"是两条规则。"""
    if not merchant:
        return None
    cleaned = _EDGE_PUNCT.sub("", re.sub(r"\s+", "", merchant))
    return cleaned or None


def is_intermediary(merchant: str | None) -> bool:
    """是不是代收机构。**用包含判定** —— 见 `PAYMENT_INTERMEDIARIES` 的说明。"""
    name = normalize(merchant)
    if not name:
        return True  # 空的当然不能进规则表
    return any(marker in name for marker in PAYMENT_INTERMEDIARIES)


def lookup(user_id: str, session: Session, *, merchant: str | None) -> MerchantRule | None:
    """查规则表。**命中会记一次 hit** —— 那个计数是判断规则有没有用的唯一依据。"""
    name = normalize(merchant)
    if not name:
        return None

    row = session.execute(_LOOKUP, {"user_id": user_id, "merchant": name}).first()
    if row is None:
        return None

    session.execute(_BUMP, {"user_id": user_id, "id": row.id})
    return _to_rule(row)


def categorize(
    user_id: str,
    session: Session,
    *,
    merchant: str | None,
    llm_category: str | None = None,
) -> Categorized:
    """两级归类:**规则优先,模型兜底。**

    `llm_category` 是记账 agent 这一轮给的答案(它已经被第 4 层复核过在枚举内)。
    规则命中时**不采用它** —— 规则是用户和历史沉淀出来的,比这一次的推断可信。
    """
    rule = lookup(user_id, session, merchant=merchant)
    if rule is not None:
        return Categorized(category=rule.category, by_rule=True, rule_id=rule.id)
    return Categorized(category=llm_category, by_rule=False)


def remember(
    user_id: str,
    session: Session,
    *,
    merchant: str | None,
    category: str,
    created_by: CreatedBy = CreatedBy.LLM,
    match_type: MatchType = MatchType.EXACT,
) -> MerchantRule | None:
    """把一次归类沉淀成规则。**拒绝就返回 None**,不抛异常。

    三种拒绝,都是常态而不是错误:

    - 代收机构名 —— ADR-008 明写不许用它污染规则表
    - 枚举外的分类 —— 和记账 agent 第 4 层同一个枚举,自由文本会让报表长草
    - 太短的名字 —— 一个字做前缀会匹配上一大片
    """
    name = normalize(merchant)
    if name is None or len(name) < MIN_MERCHANT_LENGTH:
        return None
    if category not in CATEGORIES:
        log.info("拒绝沉淀枚举外的分类:%s", category)
        return None
    if is_intermediary(name):
        # 常态,不是错误:实时那一遍本来就大多抠不出真商户
        log.debug("代收机构不进规则表:%s", name)
        return None

    row = session.execute(
        _UPSERT,
        {
            "user_id": user_id,
            "pattern": name,
            "match_type": match_type.value,
            "category": category,
            "created_by": created_by.value,
        },
    ).first()
    if row is None:
        # DO UPDATE 的 WHERE 没过:已有一条用户手改的规则,模型不许覆盖它
        return lookup(user_id, session, merchant=name)
    return _to_rule(row)


def list_rules(user_id: str, session: Session, *, limit: int = 200) -> list[MerchantRule]:
    """按命中次数倒序 —— 最上面那几条决定了报表长什么样,值得先看。"""
    rows = session.execute(_LIST, {"user_id": user_id, "limit": limit}).all()
    return [_to_rule(row) for row in rows]


def forget(user_id: str, session: Session, *, rule_id: int) -> bool:
    """删一条规则。归错类的规则要能删掉,否则只能靠新规则去盖。"""
    return session.execute(_DELETE, {"user_id": user_id, "id": rule_id}).rowcount > 0


def llm_share(counts: dict[str, int]) -> float | None:
    """**ADR-008 的监控指标:模型归类占比。**

    传进来的是一段时间内 `Categorized.by_rule` 的计数
    (`{"by_rule": 80, "by_llm": 20}`)。**这个数不随时间下降就是 bug**,
    说明沉淀那一步没生效 —— 而它不会自己报错,只会表现为账单越用越贵。
    """
    total = counts.get("by_rule", 0) + counts.get("by_llm", 0)
    if total == 0:
        return None
    return counts.get("by_llm", 0) / total


def _to_rule(row) -> MerchantRule:
    return MerchantRule(
        id=row.id,
        pattern=row.pattern,
        match_type=MatchType(row.match_type),
        category=row.category,
        created_by=CreatedBy(row.created_by),
        hit_count=row.hit_count,
    )
