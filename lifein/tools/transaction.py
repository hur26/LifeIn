"""入账的 L2 工具。**账本上每一笔 agent 记的钱都从这里过。**

06 §6.6 那张表的判据是"谁提的":用户自己点的写不过网关,agent 提的必须过。
入账没有用户点的那一路 —— 手动补一笔在 P2 第 14 片,那条是用户提的。
所以现在这个工具的调用方只有记账 job 一个,而 `tool_calls` 里那条记录
是"这笔钱哪来的、怎么撤"唯一的答案。

**为什么入账是 L2 而不是 L3。** L2 的判据是写自己的地盘、可回滚。
账本是自己的地盘;可回滚这一半靠 `transactions.undo_record()` ——
源事件还在 `raw_events` 里,删掉派生出来的那行不丢任何信息。
真正不可逆的是钱本身,而这个项目从来不碰钱(铁律 3)。

**撤销没有做成工具。** `transactions.undo_record()` 就在那里,但注册成工具的话
它会是一个谁都不能调的工具:入账是 agent 提的所以过网关,而"这笔记错了"
是用户在 App 里点的,按 06 §6.6 那张表本来就不过网关。
注册一个没有调用方的工具,和不注册的区别只有一个 —— 前者看起来像做完了。

**这个工具不判断该不该记。** 判断在记账 agent 的四层里做完了,
拿不准的根本不该走到这里,它该进 `pending_confirmations`。
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from lifein.governance.gateway import ToolOutcome
from lifein.governance.registry import ToolContext, ToolLevel, tool
from lifein.repos import transactions
from lifein.repos.transactions import CATEGORIES, Direction, Stage, TxnKind

log = logging.getLogger(__name__)


class RecordTxnArgs(BaseModel):
    occurred_at: datetime
    amount: Decimal = Field(gt=0)
    """**必须为正。** 正负由 `direction` 表达 —— 混着来的话
    "退款 -50" 和 "支出 50" 在求和时会互相抵消,而它们是两件事。"""

    direction: Direction
    kind: TxnKind
    channel: str = Field(min_length=1)
    source_event_id: int
    """`raw_events.id`。**agent 记的必须有**(铁律 5)。"""

    confidence: float = Field(ge=0.0, le=1.0)
    merchant_raw: str | None = None
    category: str | None = None
    account_hint: str | None = None
    currency: str = "CNY"
    stage: Stage = Stage.REALTIME

    @model_validator(mode="after")
    def _check(self) -> RecordTxnArgs:
        if self.occurred_at.tzinfo is None:
            # 无时区的时间会让一笔深夜的消费落到前一天,而月度报表按天切
            raise ValueError("occurred_at 必须带时区")
        if self.category is not None and self.category not in CATEGORIES:
            # 和记账 agent 第 4 层、和 merchant_rules.remember() 同一个枚举。
            # 三处都挡是因为三条路都能走到入账,而报表长草只需要一条漏网
            raise ValueError(f"分类不在枚举内:{self.category}")
        return self


@tool(
    name="txn.record",
    level=ToolLevel.L2,
    args=RecordTxnArgs,
    summary="记一笔账;跨渠道的同一笔会自动合并,不新建",
    returns_rollback=True,
)
def record(args: RecordTxnArgs, ctx: ToolContext) -> ToolOutcome:
    """入账。**两层去重在仓储里发生,这里不重复实现。**

    返回值里的 `created` 为假不代表失败:它多半是"跨渠道合并生效了"
    (支付宝和银行短信各来一条,合成一笔)。调用方要按它计数,
    否则日志里的入账条数会比账本上的笔数多。
    """
    _need_session(ctx)
    result = transactions.record(
        ctx.user_id,
        ctx.session,
        occurred_at=args.occurred_at,
        amount=args.amount,
        direction=args.direction,
        kind=args.kind,
        channel=args.channel,
        source_event_id=args.source_event_id,
        confidence=args.confidence,
        merchant_raw=args.merchant_raw,
        category=args.category,
        account_hint=args.account_hint,
        currency=args.currency,
        stage=args.stage,
    )
    return ToolOutcome(
        value={
            "transaction_id": result.transaction.id,
            "created": result.created,
            "merged_into": result.merged_into,
            "duplicate": result.duplicate,
        },
        # 回滚只认 source_event_id:那一次写入是新建、合并还是什么都没做,
        # 撤的时候不需要知道,`undo_record()` 三种都认(见它的说明)
        rollback={"undo_transaction_for_event": args.source_event_id},
    )


def _need_session(ctx: ToolContext) -> None:
    if ctx.session is None:
        raise RuntimeError("这个工具要碰库,调用方必须在 CallContext 里带上 session")
