"""待办与日程的 L2 工具 —— **本项目第一批会写东西的工具**。

L1 只读,错了顶多答得不对;L2 写自己的地盘,错了会在你的待办列表和日历里
留下东西。所以 06 §2.9 给 `tool_calls` 加了 `l2_needs_rollback` 约束,
网关也拦一道:**L2 不返回 `ToolOutcome` 就当成错误**,因为副作用已经发生、
却没有任何办法收回。

这批工具的回滚信息分两层,这是 ADR-020 带来的新情况:

    todo.create 之后        rollback = {"cancel_todo_id": ...}
                            撤销的是**服务端这一行**
    App 写进系统日历之后     todos.device_ref 有了值
                            那时候要收回的还有**设备上那条日历事件**

所以撤销一条已经同步过的日程,服务端把状态改成 `cancelled` 只完成了一半 ——
另一半是 App 下次同步时看到 `cancelled` 且 `device_ref` 非空,去把日历里那条删掉。
**服务端这行不删**:删了就没人知道设备上还有一条要清理。

**这些工具不判断"该不该建"。** 拿不准的东西根本不该走到这里,
它该进 `pending_confirmations`(第 7 片)。铁律 7 说默认行为是不动作,
而"不动作"在这里的形态是:提取 agent 不调这个工具。
"""

from __future__ import annotations

import logging
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from lifein.governance.gateway import ToolOutcome
from lifein.governance.registry import ToolContext, ToolLevel, tool
from lifein.repos import todos

log = logging.getLogger(__name__)

TITLE_MAX = 120


class CreateTodoArgs(BaseModel):
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    notes: str | None = None
    starts_at: datetime | None = None
    """给了就是日程(要写进系统日历),没给就是待办。"""

    ends_at: datetime | None = None
    provenance: list[int] = Field(default_factory=list)
    """`raw_events.id`。**agent 建的必须有**(铁律 5),库上也有 CHECK。"""

    created_by_agent: str | None = None

    @model_validator(mode="after")
    def _times_must_be_aware(self) -> CreateTodoArgs:
        # 无时区的时间会在"明天下午三点"这种事情上悄悄错八小时,
        # 而错进日历的日程比没有这个功能糟得多(03 的退出条件)
        for name in ("starts_at", "ends_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} 必须带时区")
        return self


class TodoIdArgs(BaseModel):
    todo_id: str = Field(min_length=1)


def _need_session(ctx: ToolContext) -> None:
    if ctx.session is None:
        raise RuntimeError("这个工具要碰库,调用方必须在 CallContext 里带上 session")


@tool(
    name="todo.create",
    level=ToolLevel.L2,
    args=CreateTodoArgs,
    summary="建一条待办;给了时间就是日程,会同步进手机日历",
    returns_rollback=True,
)
def create(args: CreateTodoArgs, ctx: ToolContext) -> ToolOutcome:
    """建待办或日程。**给没给时间决定它是哪一种。**

    分两个工具("建待办"和"建日程")也行,但那样 agent 每次都要先判断类型,
    而它拿到的信息里"有没有时间"本来就是同一个字段。少一次判断就少一类错。
    """
    _need_session(ctx)
    kind = todos.TodoKind.SCHEDULE if args.starts_at else todos.TodoKind.TODO
    source = todos.TodoSource.AGENT if args.created_by_agent else todos.TodoSource.USER

    created = todos.create_todo(
        ctx.user_id,
        ctx.session,
        kind=kind,
        title=args.title,
        source=source,
        starts_at=args.starts_at,
        ends_at=args.ends_at,
        notes=args.notes,
        provenance=args.provenance,
        created_by_agent=args.created_by_agent,
    )
    return ToolOutcome(
        value={"todo_id": created.id, "kind": created.kind.value},
        # 回滚是"撤销"不是"删除":删了行,设备上那条日历事件就没人管了
        rollback={"cancel_todo_id": created.id, "kind": created.kind.value},
    )


@tool(
    name="todo.complete",
    level=ToolLevel.L2,
    args=TodoIdArgs,
    summary="把一条待办标成已完成",
    returns_rollback=True,
)
def complete(args: TodoIdArgs, ctx: ToolContext) -> ToolOutcome:
    _need_session(ctx)
    before = todos.get_todo(ctx.user_id, ctx.session, todo_id=args.todo_id)
    if before is None:
        raise LookupError(f"待办不存在:{args.todo_id}")

    todos.set_status(
        ctx.user_id, ctx.session, todo_id=args.todo_id, status=todos.TodoStatus.DONE
    )
    return ToolOutcome(
        value={"todo_id": args.todo_id},
        # 记下**之前是什么状态**,不是笼统的"改回 open":
        # 把一条已经取消的待办"回滚"成待办,是又一次错误的写入
        rollback={"restore_todo_id": args.todo_id, "previous_status": before.status.value},
    )


@tool(
    name="todo.cancel",
    level=ToolLevel.L2,
    args=TodoIdArgs,
    summary="撤销一条待办或日程;已同步的会连日历里那条一起删",
    returns_rollback=True,
)
def cancel(args: TodoIdArgs, ctx: ToolContext) -> ToolOutcome:
    """撤销。**已经同步过的日程,服务端这一步只完成一半。**

    另一半在设备上:App 下次同步时看到 `cancelled` 且 `device_ref` 非空,
    去把系统日历里那条删掉。所以这里不删行 —— 删了就没人知道设备上还有一条
    要清理,而那条会一直留在你的日历里。
    """
    _need_session(ctx)
    before = todos.get_todo(ctx.user_id, ctx.session, todo_id=args.todo_id)
    if before is None:
        raise LookupError(f"待办不存在:{args.todo_id}")

    todos.set_status(
        ctx.user_id, ctx.session, todo_id=args.todo_id, status=todos.TodoStatus.CANCELLED
    )
    if before.device_ref:
        log.info("待办 %s 已同步到设备,日历里那条要等 App 下次同步才删", args.todo_id)

    return ToolOutcome(
        value={"todo_id": args.todo_id, "device_ref": before.device_ref},
        rollback={
            "restore_todo_id": args.todo_id,
            "previous_status": before.status.value,
            # 设备上那条的 id 也记进审计:回滚的回滚也要能找到它
            "device_ref": before.device_ref,
        },
    )
