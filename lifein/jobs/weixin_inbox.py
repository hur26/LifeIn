"""微信入站循环:长轮询收消息 → 交给问答 → 回复。

它只做"搬运",问答逻辑一个字都不在这里 —— `handle_message` 是通道中立的,
企微回调和这条路走的是同一个函数。

**三种失败要分开对待**,这是这个模块唯一的复杂度:

| 情况 | 怎么办 |
| --- | --- |
| 长轮询超时 | 正常节奏,立刻下一轮 |
| 网络抖动 / 限频 | 退避几秒再来 |
| **会话过期** | **停下来告警。** 继续轮询一个死掉的会话没有意义 |

第三条最要紧:会话过期之后无论轮询多少次都不会好,而"一直在轮询"看起来
和"一切正常"没有区别 —— 这正是本项目最怕的那种安静失败。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from lifein.channels import weixin_inbound
from lifein.channels.weixin import WeixinSessionExpired, WeixinUnavailable
from lifein.jobs.qa_reply import QaDeps, default_gateway, handle_message
from lifein.repos import channel_state, credentials
from lifein.scheduler import quota_checker

log = logging.getLogger(__name__)

BACKOFF_S = 5.0
MAX_CONSECUTIVE_FAILURES = 5

LEASE_KEY = "inbox_lease"
LEASE_TTL_S = 180.0
"""租约多久算死。

**一个 iLink token 同时只能有一个长轮询** —— 两个客户端一起拉会互相抢消息,
表现是"消息一会儿到一会儿不到",而两边的日志各自看起来都正常。这在一台
机器上同时跑着开发进程和正式服务时会真发生,而**那时你会以为是 iLink 在丢消息**。

三分钟不是拍的:一轮长轮询最多挂 35 秒,加上处理消息的时间,正常的一轮
远短于三分钟。而进程被 kill 之后,下一个实例最多等三分钟就能接手 ——
那三分钟里消息不会丢,长轮询的游标记着位置。
"""

SessionFactory = Callable[[], AbstractContextManager[Session]]


def run_inbox(
    user_id: str,
    *,
    services,
    session_factory: SessionFactory,
    stop: threading.Event,
    poller: weixin_inbound.WeixinPoller | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """一直收消息直到 `stop` 被置位或会话过期。

    在后台线程里跑(ADR-016 的"同进程")。每一轮都自己开事务 ——
    长轮询一挂就是几十秒,占着连接不放会把连接池耗干。
    """
    settings = services.settings

    with session_factory() as session:
        stored = credentials.get_credential(user_id, session, kind="weixin", settings=settings)
    if not stored:
        log.info("用户 %s 没配微信,不启动入站循环", user_id)
        return

    base_url = stored.get("base_url") or ""
    token = stored["token"]
    active = poller or weixin_inbound.WeixinPoller()
    failures = 0
    # 这一轮循环的身份。**每次启动都是新的** —— 进程重启之后它拿不回旧租约,
    # 只能等那条过期,而那正是要的:旧进程可能还活着
    owner = f"{socket.gethostname()}/{os.getpid()}/{uuid.uuid4().hex[:8]}"

    log.info("微信入站循环已启动,user=%s owner=%s", user_id, owner)

    while not stop.is_set():
        with session_factory() as session:
            # **每一轮都续一次租约。** 抢不到就退出 —— 另一个进程正拿着它,
            # 而两个一起拉会互相抢消息(见 LEASE_TTL_S)
            if not channel_state.claim_lease(
                user_id,
                session,
                channel=weixin_inbound.CHANNEL,
                key=LEASE_KEY,
                owner=owner,
                ttl=timedelta(seconds=LEASE_TTL_S),
            ):
                log.warning("微信入站租约被别人拿着,退出。user=%s owner=%s", user_id, owner)
                return

            sync_buf = (
                channel_state.get_state(
                    user_id,
                    session,
                    channel=weixin_inbound.CHANNEL,
                    key=weixin_inbound.SYNC_BUF_KEY,
                )
                or ""
            )

        try:
            result = active.poll_once(base_url=base_url, token=token, sync_buf=sync_buf)
        except WeixinSessionExpired as exc:
            # 停下来。继续轮询一个死掉的会话看起来和"一切正常"没有区别
            services.alerter.alert(
                "微信会话已过期",
                f"入站已停止,推送会降级到邮件。重新扫码:"
                f"python -m lifein.admin login-weixin --user {user_id}({exc})",
            )
            return
        except WeixinUnavailable as exc:
            failures += 1
            log.warning("微信长轮询失败(%d/%d):%s", failures, MAX_CONSECUTIVE_FAILURES, exc)
            if failures >= MAX_CONSECUTIVE_FAILURES:
                services.alerter.alert("微信入站连续失败", f"已退出循环:{exc}")
                return
            sleep(BACKOFF_S)
            continue

        failures = 0

        with session_factory() as session:
            # 先存游标再处理消息:反过来的话,处理到一半崩溃会把这批消息永久丢掉。
            # 现在最坏情况是漏答一次 —— 用户再问一遍就有了,比默默吞掉强
            channel_state.set_state(
                user_id,
                session,
                channel=weixin_inbound.CHANNEL,
                key=weixin_inbound.SYNC_BUF_KEY,
                value=result.sync_buf,
            )
            for sender, ctx in result.context_tokens.items():
                channel_state.set_state(
                    user_id,
                    session,
                    channel=weixin_inbound.CHANNEL,
                    key=weixin_inbound.context_token_key(sender),
                    value=ctx,
                )

        for message in result.messages:
            _handle_one(user_id, message, services=services, session_factory=session_factory)

    log.info("微信入站循环已停止,user=%s", user_id)


def _handle_one(user_id: str, message, *, services, session_factory: SessionFactory) -> None:
    del user_id  # 谁发的由 resolve_user 决定,这里不预设
    try:
        with session_factory() as session:
            handle_message(
                session,
                message=message,
                deps=QaDeps(
                    llm=services.llm,
                    channel=services.channel,
                    resolve_user=services.resolve_user,
                    gateway_factory=default_gateway,
                    # 问答是花钱最快的那条路,而它原来完全不受上限管
                    within_quota=quota_checker(services, job="weixin_inbox"),
                ),
                now=datetime.now(UTC),
            )
    except Exception:  # noqa: BLE001
        # 一条消息处理失败不该让循环退出 —— 那等于因为一句话就聋了
        log.exception("处理微信消息失败,msg_id=%s", getattr(message, "msg_id", "?"))


def start_inbox_thread(
    user_id: str, *, services, session_factory: SessionFactory
) -> tuple[threading.Thread, threading.Event]:
    """起一个守护线程跑入站循环,返回线程和停止开关。"""
    stop = threading.Event()
    thread = threading.Thread(
        target=run_inbox,
        args=(user_id,),
        kwargs={"services": services, "session_factory": session_factory, "stop": stop},
        name=f"weixin-inbox-{user_id[:8]}",
        daemon=True,
    )
    thread.start()
    return thread, stop
