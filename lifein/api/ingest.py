"""采集端的两个端点 —— [06 §6.4 / §6.5](../../docs/06-data-model.md#6-接口契约)。

**这一组只能写。** 它拿的是 `scope=ingest` 的凭据,而那种凭据在仓储层
取不出任何查询数据(R11 那句"最重要的一条")。手机丢了、App 被逆向,
拿到的采集密钥读不出你的待办、记忆和(P2 的)账本。

端点本身很薄:验签在依赖里,筛选在 `sources/notification.py`,入库在仓储。
这里只负责把它们接起来,并**如实回报每一条的去向** ——
采集端唯一能自查的东西就是这个计数,静默丢弃是这条链路最该防的失效方式(R8)。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from lifein.api.deps import Caller, IngestCaller, NowDep, SessionDep
from lifein.repos import collector, raw_events
from lifein.sources.notification import DropReason, NotificationAdapter

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingest"])

MAX_BATCH = 200
"""一次最多收多少条。

不是性能考虑 —— 是**离线补报**:采集器攒了三天再上来时,一次几万条会让
一个事务跑很久,失败还得整批重来。分批的责任在设备端,超了直接 422。
"""

ALL_DROP_REASONS = (
    DropReason.NOT_WHITELISTED,
    DropReason.PHASE_NOT_OPEN,
    DropReason.VERIFICATION_CODE,
    DropReason.MALFORMED,
    DropReason.NOT_A_TRANSACTION,
)
"""响应里**每个原因都出现,没有的填 0**。

少一个键和"这个原因是 0"在客户端看起来一样,但前者是版本对不上,
后者是真的没发生 —— 固定形状让 App 不用去猜。
"""


class NotificationIn(BaseModel):
    """一条上报。字段宽松是有意的:**形状不对的由筛选链计数丢弃**,
    不是整批 422 —— 一条坏数据不该让设备攒的另外九十九条一起退回去。"""

    model_config = ConfigDict(extra="ignore")

    channel: str = "notification"
    source_app: str | None = None
    sender: str | None = None
    posted_at: str | None = None
    title: str | None = None
    text: str | None = None
    external_id: str | None = None


class IngestBatchIn(BaseModel):
    device_id: str
    events: list[NotificationIn] = Field(default_factory=list, max_length=MAX_BATCH)


class HeartbeatIn(BaseModel):
    device_id: str
    app_version: str | None = None
    android_version: str | None = None
    listener_enabled: bool = True
    """通知监听权限还在不在。**false 也要告警** —— 进程活着但读不到东西,
    后果和掉线一样,表现却更隐蔽(06 §6.5)。"""


@router.post("/events")
def ingest_events(
    body: IngestBatchIn,
    caller: IngestCaller,
    session: SessionDep,
) -> dict[str, Any]:
    """收一批通知。返回每一条的去向。"""
    _same_device(body.device_id, caller)

    rules = collector.list_whitelist(caller.user_id, session, enabled_only=True)
    # device_id 用签名认出来的那个,不用 body 里的:后者是对方说了算的,
    # 而它要参与去重键的拼装
    adapter = NotificationAdapter(rules, device_id=caller.device_id)
    screened = adapter.screen({"events": [item.model_dump() for item in body.events]})

    result = raw_events.insert_events(caller.user_id, session, screened.events)

    dropped = {reason: screened.dropped.get(reason, 0) for reason in ALL_DROP_REASONS}
    log.info(
        "采集上报 device=%s 收下 %d 重复 %d 丢弃 %s",
        caller.device_id,
        result.inserted,
        result.duplicates,
        dropped,
    )
    return {"accepted": result.inserted, "duplicates": result.duplicates, "dropped": dropped}


@router.post("/heartbeat")
def heartbeat(
    body: HeartbeatIn,
    caller: IngestCaller,
    session: SessionDep,
    now: NowDep,
) -> dict[str, Any]:
    """心跳。`last_seen_at` 取**服务端**时间 —— 告警的判据不能由被告警的一方给。

    回一个服务端时间:设备时钟偏了会表现为"所有请求 401 且不说原因",
    那时这条是 App 唯一能自己看出问题的线索。
    """
    _same_device(body.device_id, caller)

    collector.record_heartbeat(
        caller.user_id,
        session,
        device_id=caller.device_id,
        now=now,
        app_version=body.app_version,
        android_version=body.android_version,
        listener_enabled=body.listener_enabled,
    )
    return {"server_time": now.isoformat()}


def _same_device(claimed: str, caller: Caller) -> None:
    """body 里的 device_id 必须和签名认出来的一致。

    不一致时**不静默采信签名那个**:它多半是设备端串了状态(比如恢复备份
    带过来了别人的 device_id),而静默纠正会让那个 bug 永远查不出来。
    """
    if claimed.strip() != caller.device_id:
        # 直接写 422:starlette 正在改这个常量的名字,而两个名字在不同版本里
        # 各自缺席,数字反倒是最稳的那个
        raise HTTPException(status_code=422, detail="device_id 与签名不一致")
