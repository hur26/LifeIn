"""查询端 —— App 打开时用的那一组([06 §6.3–§6.9](../../docs/06-data-model.md#6-接口契约))。

**这一组读得到东西**,所以它挂的是另一套凭据:长期设备密钥换来的短期 token,
吊销立刻生效(R11)。采集端那把密钥在这里一个字节都读不出来。

三件事在这个模块里是刻意写死的:

**用户自己点的写操作不过网关。** 铁律 4 管的是编排层,而这里是用户在自己的
地盘上动自己的数据(06 §6.6 那张表)。判据是 `todos.source`:
`agent` 建的必须过网关留下回滚信息,`user` 建的直接调仓储。

**确认待确认队列是那条规则的唯一例外。** 内容是 agent 提出来的,只是由用户
点头,所以照旧过网关,`agent` 记的是当初把它排进队列的那个。

**客户端改不了出处。** 修改后确认只接受 `title` / `notes` / `starts_at` /
`ends_at` 四项,`provenance` 与 `created_by_agent` 一律沿用队列里那份(铁律 5)。
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from lifein.api import auth
from lifein.api.deps import AppCaller, NowDep, QueryDevice, SessionDep, SettingsDep
from lifein.governance.gateway import CallContext, Gateway
from lifein.models.normalized import Trust
from lifein.repos import collector, credentials, pending, todos, users
from lifein.repos.tool_calls import PostgresAuditSink

log = logging.getLogger(__name__)

router = APIRouter(prefix="/app", tags=["app"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

TODOS_TABLE = "todos"
"""P1 唯一会写的目标表。

App 遇到不认识的 `target_table` 只展示不给确认按钮(06 §6.7),
但服务端也得挡一道 —— 客户端的克制不能当成服务端的保证。
"""

EDITABLE_FIELDS = ("title", "notes", "starts_at", "ends_at")
"""修改后确认时,客户端能改的就这几项。出处不由客户端说了算。"""


class TokenOut(BaseModel):
    token: str
    expires_at: datetime


class NewTodoIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    notes: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class StatusIn(BaseModel):
    status: str
    """`done` 或 `cancelled`。

    没有"改回 open":一条已经写进系统日历又被撤销的日程,改回 open 意味着
    要重新走一遍设备端写入,而那条路径的幂等责任在设备上(06 §6.8)。
    真要恢复,新建一条更干净。
    """


class ResolveIn(BaseModel):
    action: str
    """`confirm` 或 `reject`。"""

    payload: dict[str, Any] | None = None
    """带上就是"修改后确认",只取 `EDITABLE_FIELDS` 里那几项。"""


class CalendarReportIn(BaseModel):
    todo_id: str
    action: str
    """`created`(写进日历了)或 `deleted`(从日历删掉了)。"""

    device_ref: str | None = None
    """系统日历里那条事件的 id。`created` 时必填 —— **它就是回滚信息**。"""


class WhitelistIn(BaseModel):
    match_type: str
    pattern: str = Field(min_length=1)
    purpose: str
    phase: str = "P1"


class EnabledIn(BaseModel):
    enabled: bool


# ---------- 换 token ----------


@router.post("/token", response_model=TokenOut)
def issue_token(
    caller: QueryDevice, session: SessionDep, settings: SettingsDep, now: NowDep
) -> TokenOut:
    """长期设备凭据换短期 token(06 §6.3)。

    长期密钥只在这一个端点上网,其余请求带 token —— 每次请求都用长期密钥,
    等于把它暴露 N 倍。

    密钥在这里**再取一次**,而不是让认证依赖顺手带出来:那样每个处理函数
    都能拿到密钥,而只有这一个用得上。多一次单行查询,换的是"密钥只在
    需要它的那一个函数里出现"。
    """
    expires_at = now + timedelta(hours=settings.app_token_ttl_h)
    stored = credentials.get_device_credential(
        caller.user_id,
        session,
        kind=credentials.QUERY_KIND,
        device_id=caller.device_id,
        settings=settings,
    )
    secret = (stored or {}).get("secret", "")
    if not secret:
        # 走到这里说明凭据在验签之后、签 token 之前被吊销了。少见但真实,
        # 而它属于"这台设备已经不该有 token 了",按认证失败处理
        raise HTTPException(status_code=401)

    token = auth.mint_token(
        secret=secret,
        user_id=caller.user_id,
        device_id=caller.device_id,
        expires_at=expires_at,
    )
    return TokenOut(token=token, expires_at=expires_at)


# ---------- 待办 ----------


@router.get("/todos")
def list_todos(
    caller: AppCaller,
    session: SessionDep,
    now: NowDep,
    until: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """未完成的待办与日程。**桌面小组件读的就是它**(默认到今天结束)。"""
    horizon = until or _end_of_today(caller.user_id, session, now=now)
    items = todos.list_open(
        caller.user_id, session, until=horizon, limit=min(limit, MAX_LIMIT)
    )
    return {"until": horizon.isoformat(), "todos": [_todo_json(item) for item in items]}


@router.post("/todos")
def create_todo(caller: AppCaller, session: SessionDep, body: NewTodoIn) -> dict[str, Any]:
    """用户手动加一条。`source=user`,**不过网关**(06 §6.6)。

    用户知道自己点了什么,回滚就是再点一下。为它伪造一个 agent 名字,
    只会让审计表里多出一个查不到契约的调用方。
    """
    kind = todos.TodoKind.SCHEDULE if body.starts_at else todos.TodoKind.TODO
    try:
        created = todos.create_todo(
            caller.user_id,
            session,
            kind=kind,
            title=body.title,
            source=todos.TodoSource.USER,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            notes=body.notes,
        )
    except todos.TodoError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _todo_json(created)


@router.post("/todos/{todo_id}/status")
def set_todo_status(
    caller: AppCaller, session: SessionDep, todo_id: str, body: StatusIn
) -> dict[str, Any]:
    """完成或撤销。**撤销不删行** —— 删了就没人知道设备上还有一条要清理。"""
    if body.status not in (todos.TodoStatus.DONE, todos.TodoStatus.CANCELLED):
        raise HTTPException(status_code=422, detail="status 只能是 done 或 cancelled")

    before = _must_find(caller, session, todo_id)
    todos.set_status(
        caller.user_id, session, todo_id=todo_id, status=todos.TodoStatus(body.status)
    )
    after = todos.get_todo(caller.user_id, session, todo_id=todo_id)
    if before.device_ref and after and after.status is todos.TodoStatus.CANCELLED:
        log.info("待办 %s 已同步到设备,日历里那条等 App 下次同步才删", todo_id)
    return _todo_json(after or before)


# ---------- 待确认队列 ----------


@router.get("/pending")
def list_pending(
    caller: AppCaller, session: SessionDep, now: NowDep, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """还等着人处理的。**payload 原样给出**,渲染是展示层的事(06 §2.7)。"""
    items = pending.list_pending(
        caller.user_id, session, now=now, limit=min(limit, MAX_LIMIT)
    )
    return {"pending": [_pending_json(item) for item in items]}


@router.post("/pending/{pending_id}/resolve")
def resolve_pending(
    caller: AppCaller, session: SessionDep, pending_id: int, body: ResolveIn
) -> dict[str, Any]:
    """确认或拒绝一条。

    确认的写入与状态更新**在同一个事务里**(06 §2.7 那条硬要求)——
    `pending.confirm` 收一个写入函数就是为了让这件事在签名上成立,
    分开做的话"确认了但没写进去"迟早会发生。
    """
    if body.action not in ("confirm", "reject"):
        raise HTTPException(status_code=422, detail="action 只能是 confirm 或 reject")

    item = pending.get(caller.user_id, session, pending_id=pending_id)
    if item is None:
        raise HTTPException(status_code=404, detail="没有这一条")

    if body.action == "reject":
        # 拒绝的记录永不删除:用户拒绝过什么,正是这个 agent 最该学会不做的事
        if not pending.reject(
            caller.user_id, session, pending_id=pending_id, resolved_via="app"
        ):
            raise HTTPException(status_code=409, detail="已经处理过或已过期")
        return {"status": "rejected"}

    if item.target_table != TODOS_TABLE:
        # 这个版本的服务端只会写 todos。P2 的账目进来时,老 App 会把它列出来
        # 但不该点得动(06 §6.7)—— 真点了也要在这里挡住
        raise HTTPException(status_code=422, detail=f"还不会写 {item.target_table}")

    edited = _merge_payload(item.payload, body.payload)
    written = pending.confirm(
        caller.user_id,
        session,
        pending_id=pending_id,
        resolved_via="app",
        edited_payload=edited,
        writer=lambda payload: _create_via_gateway(caller, session, item=item, payload=payload),
    )
    if written is None:
        # 两个入口同时点确认是正常的用户行为,不是错误
        raise HTTPException(status_code=409, detail="已经处理过或已过期")
    return {"status": "edited" if edited else "confirmed", **written}


# ---------- 日历同步 ----------


@router.get("/calendar/queue")
def calendar_queue(
    caller: AppCaller, session: SessionDep, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """设备该写进系统日历的、以及该从日历里删掉的(06 §6.8)。

    两个列表一个都不能少:只给"要写的"会让撤销掉的日程永远留在日历里,
    而那正是最让人不敢信任日历的一类脏数据。
    """
    capped = min(limit, MAX_LIMIT)
    to_create = todos.unsynced_schedules(caller.user_id, session, limit=capped)
    to_delete = todos.cancelled_on_device(caller.user_id, session, limit=capped)
    return {
        "to_create": [
            {
                "todo_id": item.id,
                "title": item.title,
                "notes": item.notes,
                "starts_at": _iso(item.starts_at),
                "ends_at": _iso(item.ends_at),
            }
            for item in to_create
        ],
        "to_delete": [
            {"todo_id": item.id, "device_ref": item.device_ref} for item in to_delete
        ],
    }


@router.post("/calendar/report")
def calendar_report(
    caller: AppCaller, session: SessionDep, body: CalendarReportIn
) -> dict[str, Any]:
    """设备回报执行结果。**服务端只能保证"意图已记录",执行在设备上**(ADR-020)。"""
    _must_find(caller, session, body.todo_id)

    if body.action == "created":
        if not (body.device_ref or "").strip():
            # 没有 event id 就没有回滚信息,那条日历事件将来谁也删不掉
            raise HTTPException(status_code=422, detail="created 必须带 device_ref")
        todos.mark_synced(
            caller.user_id, session, todo_id=body.todo_id, device_ref=body.device_ref
        )
        return {"status": "synced"}

    if body.action == "deleted":
        todos.clear_device_ref(caller.user_id, session, todo_id=body.todo_id)
        return {"status": "cleared"}

    raise HTTPException(status_code=422, detail="action 只能是 created 或 deleted")


# ---------- 采集器状态与白名单 ----------


@router.get("/collector/status")
def collector_status(
    caller: AppCaller, session: SessionDep, settings: SettingsDep, now: NowDep
) -> dict[str, Any]:
    """心跳 + 白名单。

    白名单在查询端读写,不在采集端 —— 采集端只能写。App 打开时把它同步到
    本地,采集器按本地那份过滤;服务端入库前照样再过一次(架构 §8.3)。
    """
    cutoff = now - timedelta(minutes=settings.collector_heartbeat_timeout_m)
    return {
        "devices": [
            {
                "device_id": beat.device_id,
                "last_seen_at": _iso(beat.last_seen_at),
                "app_version": beat.app_version,
                "android_version": beat.android_version,
                "listener_enabled": beat.listener_enabled,
                "stale": beat.needs_alert(cutoff=cutoff),
            }
            for beat in collector.list_heartbeats(caller.user_id, session)
        ],
        "whitelist": [
            _rule_json(rule) for rule in collector.list_whitelist(caller.user_id, session)
        ],
    }


@router.post("/collector/whitelist")
def add_whitelist(caller: AppCaller, session: SessionDep, body: WhitelistIn) -> dict[str, Any]:
    if body.match_type not in (collector.MATCH_PACKAGE, collector.MATCH_SMS_SENDER):
        raise HTTPException(status_code=422, detail="match_type 不认识")
    if body.purpose not in (collector.PURPOSE_MESSAGE, collector.PURPOSE_TRANSACTION):
        raise HTTPException(status_code=422, detail="purpose 不认识")

    rule = collector.add_whitelist(
        caller.user_id,
        session,
        match_type=body.match_type,
        pattern=body.pattern,
        purpose=body.purpose,
        phase=body.phase,
    )
    return _rule_json(rule)


@router.post("/collector/whitelist/{rule_id}/enabled")
def toggle_whitelist(
    caller: AppCaller, session: SessionDep, rule_id: int, body: EnabledIn
) -> dict[str, Any]:
    """开关一条。**没有删除** —— 留着那一行能回答"曾经放行过谁"(06 §6.9)。"""
    if not collector.set_whitelist_enabled(
        caller.user_id, session, rule_id=rule_id, enabled=body.enabled
    ):
        raise HTTPException(status_code=404, detail="没有这一条")
    return {"id": rule_id, "enabled": body.enabled}


# ---------- 内部 ----------


def _create_via_gateway(caller, session, *, item: pending.Pending, payload: dict) -> dict:
    """确认之后照 payload 写进 `todos`,**过网关**。

    这是"用户的写不过网关"那条规则的唯一例外:内容是 agent 提出来的,
    用户只是点头,所以审计里得留下带 `rollback_info` 的那条记录 ——
    "这条待办哪来的、怎么撤"只有它答得上来。

    `trust=user_input`:触发这次调用的是用户的点击,不是那封邮件。
    L2 本来也不看 trust,但如实记下来才对得上"谁让建的"。
    """
    gateway = Gateway(PostgresAuditSink(caller.user_id, session))
    ctx = CallContext(
        user_id=caller.user_id,
        agent=item.agent,
        trust=Trust.USER_INPUT,
        source_event_id=item.source_event_id,
        session=session,
    )
    result = gateway.call(
        ctx,
        "todo.create",
        {
            "title": payload.get("title"),
            "notes": payload.get("notes"),
            "starts_at": payload.get("starts_at"),
            "ends_at": payload.get("ends_at"),
            "provenance": payload.get("provenance") or [],
            "created_by_agent": payload.get("created_by_agent"),
        },
    )
    return dict(result)


def _merge_payload(original: dict, edited: dict | None) -> dict | None:
    """把客户端改的那几项并回原 payload。没改就返回 None(原样确认)。

    **出处不由客户端说了算**(铁律 5):`provenance` 与 `created_by_agent`
    一律沿用队列里那份。放开的话,一条"帮张三带个东西"就能被改成
    凭空出现在待办列表里的样子,而那比不出现更让人不敢用。
    """
    if not edited:
        return None
    changes = {k: edited[k] for k in EDITABLE_FIELDS if k in edited}
    if not changes:
        return None
    return {**original, **changes}


def _must_find(caller, session, todo_id: str) -> todos.Todo:
    """找不到和不属于这个用户**不区分** —— 区分等于告诉对方"这个 id 存在"。"""
    item = todos.get_todo(caller.user_id, session, todo_id=todo_id)
    if item is None:
        raise HTTPException(status_code=404, detail="没有这一条")
    return item


def _end_of_today(user_id: str, session, *, now: datetime) -> datetime:
    """今天结束是几点,**按用户的时区**算。

    让客户端算的话,手机时区和用户时区不一致时(出差、双时区设备)
    小组件上会少一条或多一条,而那种错没人查得出来。
    """
    user = users.get_user(user_id, session)
    tz = ZoneInfo(user.tz) if user else ZoneInfo("Asia/Shanghai")
    local_end = datetime.combine(now.astimezone(tz).date(), time.max, tzinfo=tz)
    return local_end.astimezone(now.tzinfo or tz)


def _todo_json(item: todos.Todo) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind.value,
        "title": item.title,
        "notes": item.notes,
        "starts_at": _iso(item.starts_at),
        "ends_at": _iso(item.ends_at),
        "status": item.status.value,
        "source": item.source.value,
        "created_by_agent": item.created_by_agent,
        # 这两个字段是 App 上"未写入日历"那个状态的全部依据(ADR-020)
        "device_ref": item.device_ref,
        "synced_at": _iso(item.synced_at),
    }


def _pending_json(item: pending.Pending) -> dict[str, Any]:
    return {
        "id": item.id,
        "agent": item.agent,
        "kind": item.kind.value,
        "target_table": item.target_table,
        "payload": item.payload,
        "reason": item.reason.value,
        "confidence": item.confidence,
        "expires_at": _iso(item.expires_at),
    }


def _rule_json(rule: collector.WhitelistRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "match_type": rule.match_type,
        "pattern": rule.pattern,
        "purpose": rule.purpose,
        "enabled": rule.enabled,
        "phase": rule.phase,
    }


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None
