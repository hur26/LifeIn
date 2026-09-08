"""记忆检索工具 —— 本项目的**第一批注册工具**,全是 L1。

P1 验收标准第一条是"能正确回答:上次和 X 聊的是什么、我这周答应了谁什么事"。
这三个工具就是那句话的可执行版本:

    memory.search_entities    你认识的这个人是谁(名字 → 实体)
    memory.recent_events_with 上次和他往来的是什么(实体 → 事件)
    memory.recall_facts       关于这件事我们已经有什么结论(记忆里的事实)

**为什么是工具而不是直接调仓储。** 编排层直接 import 仓储也能跑,但那样
这三次读取就不会出现在 `tool_calls` 里 —— 而"它凭什么这么回答"要能被复盘,
靠的正是那张表。过网关还顺带拿到 (agent, tool) 双重门:问答能查记忆,
不代表将来每个 agent 都能查。

**全部 L1:只读,自动放行。** 它们一个字节都不写。写记忆的是抽取任务,
那条路径不经过工具 —— 它是系统自己的沉淀,不是谁"调用"出来的。

返回值是给模型看的**扁平结构**,不是 ORM 对象:每一项都带得回 `external_id`
或 `fact_id`,答案才指得回来源。指不回来源的回答在问答里会被标成没有依据。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from lifein.governance.registry import ToolContext, ToolLevel, tool
from lifein.repos import embeddings, entities, facts, raw_events

log = logging.getLogger(__name__)

MAX_ENTITIES = 5
MAX_EVENTS = 10
MAX_FACTS = 20

BODY_PREVIEW = 300
"""正文只回这么多字。

工具的返回值最终会进 prompt,而这里回多少直接决定发给外部供应商多少
(R12)。回全文既贵又没必要:问答要的是"上次聊了什么",一段开头就够定位。
"""


class SearchEntitiesArgs(BaseModel):
    name: str = Field(min_length=1)


class RecentEventsArgs(BaseModel):
    name: str = Field(min_length=1)
    limit: int = Field(default=MAX_EVENTS, ge=1, le=MAX_EVENTS)


class RecallFactsArgs(BaseModel):
    query: str = ""
    """留空就是"把当前成立的事实都给我",用于"我这周答应了谁什么事"这类问题。"""

    limit: int = Field(default=MAX_FACTS, ge=1, le=MAX_FACTS)

    query_embedding: list[float] | None = None
    """问句的向量。**给了就先做模糊召回**,没给就只有字面检索(ADR-019)。

    向量由调用方算 —— 算它要 LLM 客户端,而工具只拿得到 user_id 和 session。
    这不是权宜:工具能自己调外部服务的话,"这次调用花了多少钱"就再也说不清了。
    """

    embedding_model: str | None = None
    """算这个向量用的模型。**必须和向量一起给**:不同模型的向量之间比距离
    会得到一个看起来正常、实际无意义的数(06 §2.4)。"""


def _need_session(ctx: ToolContext) -> None:
    if ctx.session is None:
        # 工具不自己开事务(见 ToolContext)。没给就是调用方的 bug,当场炸,
        # 不要退化成"自己连一个" —— 那会让 L2 的同事务约束悄悄失效
        raise RuntimeError("这个工具要碰库,调用方必须在 CallContext 里带上 session")


@tool(
    name="memory.search_entities",
    level=ToolLevel.L1,
    args=SearchEntitiesArgs,
    summary="按名字查我认识的人或商户",
)
def search_entities(args: SearchEntitiesArgs, ctx: ToolContext) -> list[dict[str, Any]]:
    _need_session(ctx)
    found = entities.search_entities(
        ctx.user_id, ctx.session, name=args.name, limit=MAX_ENTITIES
    )
    return [
        {
            "entity_id": e.id,
            "name": e.canonical_name,
            "kind": e.kind.value,
            "last_seen_at": e.last_seen_at.isoformat(),
        }
        for e in found
    ]


@tool(
    name="memory.recent_events_with",
    level=ToolLevel.L1,
    args=RecentEventsArgs,
    summary="查最近和某个人往来的邮件与日程",
)
def recent_events_with(args: RecentEventsArgs, ctx: ToolContext) -> dict[str, Any]:
    """先把名字归到实体,再按那个实体的**全部别名**去找事件。

    差别在这里:直接拿用户打的"老张"去搜正文,搜不到他署名"张伟"的那些邮件;
    过一遍实体,他换过的邮箱、用过的署名都会被算进来。

    **重名不替用户选。** 命中多个实体时只用最近见过的那个,其余的名字原样
    回给调用方,让答案能说"你是指哪个李工" —— 猜一个然后自信地答错更糟。
    """
    _need_session(ctx)
    matches = entities.search_entities(
        ctx.user_id, ctx.session, name=args.name, limit=MAX_ENTITIES
    )
    if not matches:
        return {"entity": None, "events": [], "other_matches": []}

    target = matches[0]
    aliases = entities.list_aliases(ctx.user_id, ctx.session, entity_id=target.id)

    identifiers = [a.alias for a in aliases if a.alias_type is not entities.AliasType.NAME]
    names = [a.alias for a in aliases if a.alias_type is entities.AliasType.NAME]
    names.append(target.canonical_name)

    found = raw_events.fetch_events_with_party(
        ctx.user_id,
        ctx.session,
        identifiers=identifiers,
        names=names,
        limit=args.limit,
    )

    return {
        "entity": {"entity_id": target.id, "name": target.canonical_name},
        "events": [
            {
                "source": item.event.external_ref.source,
                "external_id": item.event.external_ref.external_id,
                "title": item.event.title,
                "occurred_at": item.event.occurred_at.isoformat(),
                "body": (item.event.body or "")[:BODY_PREVIEW],
            }
            for item in found
        ],
        "other_matches": [m.canonical_name for m in matches[1:]],
    }


def _similar_facts(args: RecallFactsArgs, ctx: ToolContext, *, at: datetime) -> list:
    """向量召回。**任何一步缺席都安静地返回空** —— 退回字面检索(ADR-019)。

    缺席的形态:没配 EMBEDDING_MODEL、这次没算出向量、库里还一条向量都没有。
    它们都不是故障,只是这次答得浅一点。
    """
    if not args.query_embedding or not args.embedding_model:
        return []

    hits = embeddings.search(
        ctx.user_id,
        ctx.session,
        query_embedding=args.query_embedding,
        model=args.embedding_model,
        ref_type=embeddings.RefType.FACT,
        limit=args.limit,
    )
    if not hits:
        return []

    by_id = {
        f.id: f
        for f in facts.get_facts_by_ids(
            ctx.user_id, ctx.session, fact_ids=[h.ref_id for h in hits], at=at
        )
    }
    # 保持向量给出的顺序:越靠前越像。get_facts_by_ids 的返回顺序是库说了算的
    return [by_id[h.ref_id] for h in hits if h.ref_id in by_id]


@tool(
    name="memory.recall_facts",
    level=ToolLevel.L1,
    args=RecallFactsArgs,
    summary="回忆已经沉淀下来的事实",
)
def recall_facts(args: RecallFactsArgs, ctx: ToolContext) -> list[dict[str, Any]]:
    """取事实。**每一条都带 provenance 和置信度一起回。**

    不带这两样的话,一条 0.4 分、来自一封群发邮件的推断,在 prompt 里看起来
    和用户亲口确认过的一模一样 —— 那正是记忆污染变得无法追查的方式。
    """
    _need_session(ctx)
    now = datetime.now(UTC)

    found = _similar_facts(args, ctx, at=now)
    seen = {f.id for f in found}

    if args.query.strip():
        literal = facts.search_facts(
            ctx.user_id, ctx.session, query=args.query, at=now, limit=args.limit
        )
    else:
        literal = facts.list_active_facts(ctx.user_id, ctx.session, at=now, limit=args.limit)

    # 向量召回在前、字面在后:前者是"意思像的",后者是"字面撞上的",
    # 而超出 limit 被切掉时该先保住前者
    found.extend(f for f in literal if f.id not in seen)
    found = found[: args.limit]

    return [
        {
            "fact_id": f.id,
            "statement": f.statement,
            "confidence": f.confidence,
            "confirmed_by_user": f.confirmed_by_user,
            "provenance": f.provenance,
            "valid_from": f.valid_from.isoformat(),
        }
        for f in found
    ]
