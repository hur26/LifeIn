"""自己关掉采集、自己删掉已采数据(P4 第 3 片)。

[R10 那节的改判](../../docs/05-risks.md#r10--手机端采集器的越权读取)写着四个前提,
**少一件就不该开放**,而这个模块是第 2 件:

> 朋友要能自己关掉采集、并删掉已采的数据。**App 里要有这个开关,
> 不是"找你帮忙"。**

"找你帮忙"和"自己能做"的差别不在功能,在**是不是要开口**。一个人要发一条
微信才能删掉自己的数据时,他多半不会发那条微信 —— 而那时"他能删"就只是一句话。

## 关掉采集是三件事,不是一件

| 层 | 做什么 | 关掉之后 |
| --- | --- | --- |
| 手机端 | 停止上报 | 通知根本不离开手机 |
| **凭据** | 吊销采集密钥 | 就算 App 有 bug 还在报,服务端也不收 |
| 白名单 | 全部停用 | 密钥万一还在,也没有来源被放行 |

**只做手机端那一层是不够的**:App 有 bug、被降级、被别人装了旧版本,
上报都可能继续。所以关掉的动作在服务端**同时**吊销密钥并停白名单 ——
这样"关掉了"是一个服务端能自己保证的状态,而不是一个对客户端的期望。

## 删除是真删

不是标记。`raw_events` 里的通知原文、由它们派生的交易、待确认、
`facts` 的出处 —— 该删的全删。**留一份"以防万一"就等于没删**,
而那正是隐私说明里最不该出现的一句话。

**但派生的东西要一起走。** 只删 `raw_events` 会留下一堆
`provenance` 指向不存在事件的记忆条目,而那些条目**看起来仍然是有出处的** ——
点开才发现出处没了。那比留着原文更糟:它让"有出处"这件事变得不可信。

## 派生的东西到底有哪些

**这份清单最初漏了三样**,而漏掉的表现是"删除按钮点了,东西还在":

| 表 | 怎么关联 | 漏掉的后果 |
| --- | --- | --- |
| `transactions` | `source_event_id` | —— |
| `pending_confirmations` | `source_event_id` | —— |
| `facts` | `provenance` 全落在被删事件里 | —— |
| **`todos`** | `provenance` 全落在被删事件里 | 日程还在日历里,说"来自某条通知",而那条通知没了 |
| **`embeddings`** | `ref_id` 指向被删的事件或事实 | **向量是原文的有损编码。**
"删了但向量还在"不算删掉,而且它还能被语义检索命中 |
| **`entity_aliases`** | `evidence_event_ids` 里有被删事件 |
别名上写着"证据是第 42 条事件",而第 42 条不存在了 |

**顺序不是随手排的,两处有讲究:**

- `raw_events` 最后。外键指着它,先删它数据库直接拒绝
- **事实的向量要在事实之前删。** 它们之间没有外键(`ref_id` 是 TEXT),
  所以数据库不会拦 —— 但"哪些事实的出处全没了"这个子查询在事实删完之后
  查不到任何东西,于是那些向量留了下来,而且还能被语义检索命中

## 有两样东西刻意留着

- **`tool_calls`**:审计。里面只有字段名和长度,没有内容 ——
  而删掉它等于让"它到底把什么发给了外部模型"这个问题永远没法回答(R12)。
  **这是保护用户的记录,不是关于用户的记录。**
- **`push_log`**:同样只有标题和正文长度。它是误报率唯一的来源,
  而那个数字决定要不要继续推东西。

两样都不含通知原文。要连它们一起清掉只有一条路:整个注销账户
(`export.purge_user`,那份清单是全的)。**这一点写进了隐私说明**,
不能只在代码里知道。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifein.repos import collector, credentials

log = logging.getLogger(__name__)

COLLECT_SOURCES = ("notification",)
"""哪些来源算"采集来的"。**邮件和日历不在里面** ——
它们是你自己的账号,而这个开关关的是手机上那一路(R10 说的就是它)。"""


@dataclass(frozen=True)
class CollectionState:
    enabled: bool
    """还在采不在采。**三层里任何一层关着就算关**(见模块开头)。"""

    active_devices: int
    enabled_rules: int


@dataclass(frozen=True)
class Deleted:
    raw_events: int
    transactions: int
    pending: int
    facts: int
    """出处只剩被删事件的记忆条目。**跟着一起删** ——
    留着的话它们看起来仍然有出处,点开才发现没了。"""

    todos: int = 0
    """出处只剩被删事件的待办与日程。理由和 `facts` 一样。"""

    embeddings: int = 0
    """被删事件与事实的向量。**"删了但向量还在"不算删掉** ——
    向量是原文的有损编码,而且它还能被语义检索命中。"""

    aliases: int = 0
    """证据被清空之后整条删掉的别名。证据里只是**少了几条**的不删,
    那个别名还有别的依据。"""

    def total(self) -> int:
        return (
            self.raw_events
            + self.transactions
            + self.pending
            + self.facts
            + self.todos
            + self.embeddings
            + self.aliases
        )


_COUNT_ACTIVE_COLLECTORS = text("""
    SELECT count(*) FROM credentials
     WHERE user_id = :user_id AND kind = 'collector' AND revoked_at IS NULL
""")

_SELECT_EVENTS = text("""
    SELECT id FROM raw_events
     WHERE user_id = :user_id
       AND source = ANY(:sources)
       AND (CAST(:since AS TIMESTAMPTZ) IS NULL OR occurred_at >= CAST(:since AS TIMESTAMPTZ))
""")

_DELETE_EVENTS = text("""
    DELETE FROM raw_events
     WHERE user_id = :user_id AND id = ANY(CAST(:event_ids AS BIGINT[]))
""")

_DELETE_TXNS = text("""
    DELETE FROM transactions
     -- CAST 不能省:小整数的数组参数会被推成 smallint[],和 bigint 比不了。
     -- 报的是 operator does not exist,不是"你写错了"
     WHERE user_id = :user_id AND source_event_id = ANY(CAST(:event_ids AS BIGINT[]))
""")

_DELETE_PENDING = text("""
    DELETE FROM pending_confirmations
     -- CAST 不能省:小整数的数组参数会被推成 smallint[],和 bigint 比不了。
     -- 报的是 operator does not exist,不是"你写错了"
     WHERE user_id = :user_id AND source_event_id = ANY(CAST(:event_ids AS BIGINT[]))
""")

_DELETE_ORPHAN_FACTS = text("""
    DELETE FROM facts
     WHERE user_id = :user_id
       -- 出处**全部**落在被删的事件里才删。还有别的出处的留着 ——
       -- 那条事实不只来自被删掉的这些
       AND provenance <@ CAST(:event_ids AS BIGINT[])
       AND array_length(provenance, 1) > 0
""")


_DELETE_ORPHAN_TODOS = text("""
    DELETE FROM todos
     WHERE user_id = :user_id
       -- 和 facts 同一条判据:出处**全部**落在被删事件里才删。
       -- 还有别的出处的留着 —— 那条日程不只来自被删掉的这些
       AND provenance <@ CAST(:event_ids AS BIGINT[])
       AND array_length(provenance, 1) > 0
""")

_DELETE_EVENT_VECTORS = text("""
    DELETE FROM embeddings
     WHERE user_id = :user_id
       AND ref_type = 'raw_event'
       -- ref_id 是 TEXT(它要同时指得了 bigint 的事件和 uuid 的事实),
       -- 所以这里把事件 id 转成文本来比,不是把它转成 bigint
       AND ref_id = ANY(CAST(:event_ids AS TEXT[]))
""")

_DELETE_ORPHAN_FACT_VECTORS = text("""
    DELETE FROM embeddings
     WHERE user_id = :user_id
       AND ref_type = 'fact'
       -- **必须在删 facts 之前跑。** 删完之后就找不到"哪些事实没了",
       -- 而留下来的向量还能被语义检索命中 —— 那是"删了但没删干净"
       AND ref_id IN (
            SELECT CAST(id AS TEXT) FROM facts
             WHERE user_id = :user_id
               AND provenance <@ CAST(:event_ids AS BIGINT[])
               AND array_length(provenance, 1) > 0
       )
""")

_STRIP_ALIAS_EVIDENCE = text("""
    UPDATE entity_aliases
       SET evidence_event_ids = ARRAY(
            SELECT e FROM unnest(evidence_event_ids) AS e
             WHERE e <> ALL(CAST(:event_ids AS BIGINT[]))
       )
     WHERE user_id = :user_id
       AND evidence_event_ids && CAST(:event_ids AS BIGINT[])
""")
"""把被删事件从证据数组里摘出去。**改不是删** —— 一个别名可能有好几条证据,
删掉整条等于把别的证据也一起丢了。"""

_DELETE_EMPTY_ALIASES = text("""
    DELETE FROM entity_aliases
     WHERE user_id = :user_id AND cardinality(evidence_event_ids) = 0
""")
"""证据被清空的别名整条删掉。

**一条没有任何证据的别名比没有这条别名更糟**:它会继续把"老王"解析到某个
实体上,而没有任何东西能解释凭什么。"""


def state(user_id: str, session: Session) -> CollectionState:
    """现在在不在采。**三层里任何一层关着就算关。**"""
    devices = session.execute(_COUNT_ACTIVE_COLLECTORS, {"user_id": user_id}).scalar_one()
    rules = [r for r in collector.list_whitelist(user_id, session) if r.enabled]
    return CollectionState(
        enabled=bool(devices) and bool(rules),
        active_devices=int(devices),
        enabled_rules=len(rules),
    )


def stop_collecting(user_id: str, session: Session) -> CollectionState:
    """关掉采集。**服务端这边做完就算数,不等 App 配合。**

    吊销采集密钥 + 停用全部白名单。这两件都做,是因为它们各自挡住不同的
    失效方式:密钥没了但白名单还开着,换一台设备重新配码就又开始采;
    白名单空了但密钥还在,加一条规则就又开始采。

    **不删已采的数据** —— 那是另一个动作(`delete_collected`)。
    合成一个的话,"我想先停下来想想"就变成了"要么继续采要么全删"。
    """
    revoked = credentials.revoke_all_of_kind(user_id, session, kind="collector")
    disabled = collector.disable_all(user_id, session)
    log.info("采集已关闭:user=%s 吊销 %s 条凭据,停用 %s 条白名单", user_id, revoked, disabled)
    return state(user_id, session)


def delete_collected(
    user_id: str, session: Session, *, since: datetime | None = None
) -> Deleted:
    """删掉采集来的数据。**是真删,不是标记。**

    `since` 不给就是全部。给了就只删那之后的 —— "把上周那几天删掉"是一个
    真实的诉求,而**只能全删的删除按钮很多人不敢点**。

    派生的东西一起走(见模块开头):交易、待确认、以及出处**全部**落在
    被删事件里的记忆条目。
    """
    event_ids = [
        row.id
        for row in session.execute(
            _SELECT_EVENTS,
            {"user_id": user_id, "sources": list(COLLECT_SOURCES), "since": since},
        ).all()
    ]
    if not event_ids:
        return Deleted(raw_events=0, transactions=0, pending=0, facts=0)


    # **顺序不能反。** `transactions.source_event_id` 和
    # `pending_confirmations.source_event_id` 都有指向 `raw_events` 的外键 ——
    # 先删事件的话数据库直接拒绝,而那个报错("violates foreign key constraint")
    # 完全不解释"你应该先删派生的那些"
    args = {"user_id": user_id, "event_ids": event_ids}
    txt_args = {"user_id": user_id, "event_ids": [str(i) for i in event_ids]}

    txns = session.execute(_DELETE_TXNS, args).rowcount
    pending = session.execute(_DELETE_PENDING, args).rowcount
    todos = session.execute(_DELETE_ORPHAN_TODOS, args).rowcount

    # **向量在事实之前删。** 反过来的话那条子查询("哪些事实的出处全没了")
    # 已经查不到任何东西,而留下来的向量还能被语义检索命中
    vectors = session.execute(_DELETE_ORPHAN_FACT_VECTORS, args).rowcount
    vectors += session.execute(_DELETE_EVENT_VECTORS, txt_args).rowcount

    facts = session.execute(_DELETE_ORPHAN_FACTS, args).rowcount

    session.execute(_STRIP_ALIAS_EVIDENCE, args)
    aliases = session.execute(_DELETE_EMPTY_ALIASES, {"user_id": user_id}).rowcount

    session.execute(_DELETE_EVENTS, args)

    log.info(
        "已删除采集数据:user=%s 事件 %s 交易 %s 待确认 %s 待办 %s 记忆 %s 向量 %s 别名 %s",
        user_id, len(event_ids), txns, pending, todos, facts, vectors, aliases,
    )
    return Deleted(
        raw_events=len(event_ids),
        transactions=txns,
        pending=pending,
        facts=facts,
        todos=todos,
        embeddings=vectors,
        aliases=aliases,
    )
