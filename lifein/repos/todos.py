"""`todos` 的读写 —— 待办,以及"等着被写进手机系统日历"的日程。

ADR-020:落地表在服务端,**App 是它的界面,不是另一个数据源**。换手机不丢。

这张表和别处最不一样的一点:`kind=schedule` 的行,**副作用发生在服务端之外**。
服务端写完只代表"意图已记录",真正写进系统日历的是 App,写完回报 `device_ref`。
所以这里有两个状态而不是一个:

    行写进来了            create_todo 返回,L2 工具的回滚信息拿得到 id
    设备确认写进日历了     mark_synced 之后,device_ref 有值

**`synced_at` 为空就是还没落地。** 不要把它读成"同步中" —— 手机可能三天没开机,
也可能 App 被卸载了。这个状态要能被查出来(`unsynced_schedules`),
因为"以为写进日历了其实没有"是这条链路唯一不可接受的失败方式。

写入规则里有一条是铁律 5 的延伸:**agent 建的条目必须带 provenance**,
库上有 CHECK。用户自己加的不需要 —— 他就是出处。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


class TodoKind(StrEnum):
    TODO = "todo"
    """没有确定时间的事。留在待办列表和小组件里。"""

    SCHEDULE = "schedule"
    """有确定时间的事。要由 App 写进系统日历。"""


class TodoStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"
    """撤销。**不删行** —— 回滚一次 L2 调用要留得下痕迹,删了审计就指向空。"""


class TodoSource(StrEnum):
    AGENT = "agent"
    USER = "user"


class TodoError(ValueError):
    """写不进去。都在进库前抛,带得出人话的原因。"""


@dataclass(frozen=True)
class Todo:
    id: str
    kind: TodoKind
    title: str
    notes: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    status: TodoStatus
    source: TodoSource
    provenance: list[int]
    device_ref: str | None
    synced_at: datetime | None
    created_by_agent: str | None

    @property
    def needs_device_sync(self) -> bool:
        """要写进系统日历、但设备还没确认过。"""
        return self.kind is TodoKind.SCHEDULE and self.synced_at is None


_COLUMNS = """
    id, kind, title, notes, starts_at, ends_at, status, source,
    provenance, device_ref, synced_at, created_by_agent
"""

_INSERT = text(f"""
    INSERT INTO todos
        (user_id, kind, title, notes, starts_at, ends_at, source, provenance, created_by_agent)
    VALUES
        (:user_id, :kind, :title, :notes, :starts_at, :ends_at, :source, :provenance, :agent)
    RETURNING {_COLUMNS}
""")

_SELECT_ONE = text(f"SELECT {_COLUMNS} FROM todos WHERE user_id = :user_id AND id = :todo_id")

_SET_STATUS = text("""
    UPDATE todos
       SET status = :status, updated_at = now()
     WHERE user_id = :user_id AND id = :todo_id AND status <> :status
""")

_MARK_SYNCED = text("""
    UPDATE todos
       SET device_ref = :device_ref, synced_at = now(), updated_at = now()
     WHERE user_id = :user_id AND id = :todo_id
""")

_LIST_OPEN = text(f"""
    SELECT {_COLUMNS}
      FROM todos
     WHERE user_id = :user_id
       AND status = 'open'
       AND (starts_at IS NULL OR starts_at < :until)
     ORDER BY starts_at NULLS LAST, created_at
     LIMIT :limit
""")

_LIST_UNSYNCED = text(f"""
    SELECT {_COLUMNS}
      FROM todos
     WHERE user_id = :user_id
       AND kind = 'schedule'
       AND status = 'open'
       AND synced_at IS NULL
     ORDER BY starts_at
     LIMIT :limit
""")


def create_todo(
    user_id: str,
    session: Session,
    *,
    kind: TodoKind,
    title: str,
    source: TodoSource,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    notes: str | None = None,
    provenance: Sequence[int] = (),
    created_by_agent: str | None = None,
) -> Todo:
    """建一条待办或日程。

    三道在进库前拦的检查,和库上的 CHECK 一一对应 —— 库那道是防线,
    这道给的是人话。**报错信息是半夜排查时的唯一线索**,和 `facts` 那边同理。
    """
    if not title.strip():
        raise TodoError("title 不能为空:列表和小组件全靠它")
    if kind is TodoKind.SCHEDULE and starts_at is None:
        raise TodoError("日程必须有开始时间,否则设备端那次写入必然失败")
    if source is TodoSource.AGENT and not provenance:
        raise TodoError(f"agent 建的条目必须说得出出处(铁律 5):{title[:40]}")
    if source is TodoSource.AGENT and not (created_by_agent or "").strip():
        raise TodoError("agent 建的条目要记下是哪个 agent 建的")
    if starts_at and ends_at and ends_at < starts_at:
        raise TodoError("结束时间早于开始时间")

    row = session.execute(
        _INSERT,
        {
            "user_id": user_id,
            "kind": kind.value,
            "title": title.strip(),
            "notes": notes,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "source": source.value,
            "provenance": list(provenance),
            "agent": created_by_agent,
        },
    ).one()
    return _to_todo(row)


def set_status(user_id: str, session: Session, *, todo_id: str, status: TodoStatus) -> bool:
    """改状态。**撤销也走这里,不删行。**

    删掉的话,审计里那条 L2 调用的 `rollback_info` 就指向一个不存在的 id ——
    "这次操作被回滚过"和"这次操作从没发生过"是两回事,后者查不出来。
    """
    result = session.execute(
        _SET_STATUS, {"user_id": user_id, "todo_id": todo_id, "status": status.value}
    )
    return result.rowcount > 0


def mark_synced(user_id: str, session: Session, *, todo_id: str, device_ref: str) -> bool:
    """设备回报:已经写进系统日历了,那条事件的 id 是 `device_ref`。

    **这是 L2 回滚信息真正的落点。** 服务端删掉自己这行不算回滚 ——
    日历里那条还在。回滚要靠这个 id 让 App 去删。
    """
    if not device_ref.strip():
        raise TodoError("device_ref 不能为空:没有它就没法回滚设备上那条事件")
    result = session.execute(
        _MARK_SYNCED, {"user_id": user_id, "todo_id": todo_id, "device_ref": device_ref.strip()}
    )
    return result.rowcount > 0


def get_todo(user_id: str, session: Session, *, todo_id: str) -> Todo | None:
    row = session.execute(_SELECT_ONE, {"user_id": user_id, "todo_id": todo_id}).first()
    return _to_todo(row) if row else None


def list_open(user_id: str, session: Session, *, until: datetime, limit: int = 50) -> list[Todo]:
    """待办列表与桌面小组件的数据来源。

    没有时间的排在有时间的后面(`NULLS LAST`):小组件上方寸之地,
    今天几点要到场的事比"有空做"的事重要。
    """
    rows = session.execute(
        _LIST_OPEN, {"user_id": user_id, "until": until, "limit": limit}
    ).all()
    return [_to_todo(row) for row in rows]


def unsynced_schedules(user_id: str, session: Session, *, limit: int = 50) -> list[Todo]:
    """还没写进系统日历的日程。App 每次同步问的就是这个。

    它同时是那条"看得见的延迟"的依据:这个列表长期不空,说明设备端出了问题,
    而不是没有日程要写。
    """
    rows = session.execute(_LIST_UNSYNCED, {"user_id": user_id, "limit": limit}).all()
    return [_to_todo(row) for row in rows]


def _to_todo(row) -> Todo:
    return Todo(
        id=str(row.id),
        kind=TodoKind(row.kind),
        title=row.title,
        notes=row.notes,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        status=TodoStatus(row.status),
        source=TodoSource(row.source),
        provenance=list(row.provenance or []),
        device_ref=row.device_ref,
        synced_at=row.synced_at,
        created_by_agent=row.created_by_agent,
    )
