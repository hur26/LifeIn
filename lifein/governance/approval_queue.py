"""网关和 `approvals` 表之间那一层(P3 第 3 片)。

网关只认一个 `ApprovalQueue` 协议(`gateway.py` 里那个),而这里是它唯一的
真实实现。分开是为了**网关的测试不需要数据库** —— 而网关那几条不变量
(L3 必须 `user_input`、每次调用留审计)恰恰是最该被大量用例覆盖的。

## 幂等键从哪来

网关不知道"同一个意图"是什么,所以它不生成幂等键。这里也不生成 ——
`approvals.enqueue()` 的兜底会按内容算一个,而**那只挡得住一字不差的重复**。

真正的幂等键要由**提出这次调用的那一层**给:比如问答 agent 知道
"回复老王那条消息"是同一件事,哪怕你两次说法不同。它通过
`CallContext.idempotency_key` 往下传 —— 那个字段是这一片加的。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from lifein.governance.gateway import CallContext
from lifein.governance.registry import ToolSpec
from lifein.repos import approvals

log = logging.getLogger(__name__)


class PostgresApprovalQueue:
    """把 L3 调用写进 `approvals`。**网关拿到的 id 是这张表的主键。**"""

    def __init__(self, session: Session, *, now: datetime) -> None:
        self._session = session
        self._now = now
        # 时间由调用方给:审批的过期是安全属性,而"现在几点"在测试里
        # 必须能定住 —— 定不住的话"过期之后点不动"这条根本测不了

    def enqueue(
        self,
        *,
        ctx: CallContext,
        spec: ToolSpec,
        args: Any,
        preview_text: str,
    ) -> str:
        item = approvals.enqueue(
            ctx.user_id,
            self._session,
            agent=ctx.agent,
            tool_name=spec.name,
            tool_args=_as_dict(args),
            preview_text=preview_text,
            trust=ctx.trust,
            now=self._now,
            idempotency_key=ctx.idempotency_key,
            source_event_id=ctx.source_event_id,
        )
        log.info("L3 进审批队列:%s #%s", spec.name, item.id)
        # 网关那边把它当字符串用(协议就是这么定的),而库里是 bigint
        return str(item.id)


def _as_dict(args: Any) -> dict:
    """把校验过的入参变回可入库的形状。

    `model_dump(mode="json")` 而不是 `dict()`:参数里有 `datetime` 和
    `Decimal`,而**它们要能原样读回来** —— 审批通过之后执行那一步拿的就是这份,
    读回来差一秒或差一分钱都是另一件事。
    """
    if isinstance(args, BaseModel):
        return args.model_dump(mode="json")
    return dict(args) if isinstance(args, dict) else {"value": str(args)}
