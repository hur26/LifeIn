"""HTTP 入口。

三组端点,**各自的认证面完全分开**(06 §6.1):

| 组 | 谁在调 | 认证 |
| --- | --- | --- |
| `/wecom/*` | 企微平台 | 平台签名 |
| `/ingest/*` | 手机上的采集器 | 设备密钥签名,**只能写** |
| `/app/*` | 手机上的界面 | 长期设备凭据换来的短期 token |

**错误响应一律不带原因。** 企微回调回 400、App 那两组回 401,
里面写的是空的 —— 具体是签名不对还是时间戳过期只进日志。
告诉探测者他哪一步错了,等于帮他调试。

默认只监听 `127.0.0.1`(07 §2.1),公网访问走反向代理 + TLS。
反代**不能改写路径** —— 路径进签名(06 §6.2)。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response, status

from lifein.api import ingest
from lifein.api.deps import AuthRejected
from lifein.bootstrap import Services, build_services
from lifein.channels.wecom_callback import CallbackRejected
from lifein.db import session_scope
from lifein.jobs.qa_reply import QaDeps, default_gateway, handle_message
from lifein.jobs.weixin_inbox import start_inbox_thread
from lifein.repos import users
from lifein.scheduler import build_scheduler, run_digest_for_all_users

log = logging.getLogger(__name__)


def create_app(services: Services | None = None, *, with_scheduler: bool = False) -> FastAPI:
    """建应用。

    `with_scheduler=True` 时把调度器挂进 lifespan —— 一个进程既收回调又跑
    定时任务,这是 ADR-016"同进程"的取向。默认关着,因为测试不该起后台线程。
    """
    resolved = services or build_services()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        scheduler = None
        inbox_stops: list = []
        if with_scheduler:
            scheduler = build_scheduler(resolved)
            scheduler.start()
            # 启动即跑一次:昨晚八点进程正好挂着,今早重启不该等到明晚才发现
            # 漏了一天。算不出窗口时它什么都不做
            try:
                run_digest_for_all_users(resolved)
            except Exception:  # noqa: BLE001
                log.exception("启动补跑失败")

            # 微信入站:每个配了微信的用户一个线程。没配的会自己退出,
            # 所以这里不判断配没配
            inbox_stops = _start_inboxes(resolved)
        try:
            yield
        finally:
            for stop in inbox_stops:
                stop.set()
            if scheduler is not None:
                scheduler.shutdown(wait=False)

    # 关掉自动文档:这个服务只服务于两个已知的调用方,把端点和 schema
    # 挂在公网上是白送的侦察信息。
    # openapi_url 必须一起关 —— 只关 docs_url,/openapi.json 照样是公开的,
    # 而那份 JSON 比页面本身更好用。
    app = FastAPI(
        title="LifeIn", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.services = resolved
    app.include_router(ingest.router)

    @app.exception_handler(AuthRejected)
    def _auth_rejected(_request: Request, exc: AuthRejected) -> Response:
        # 原因只进日志。响应体是空的 —— 401 之外不给对方任何信息
        log.warning("拒绝一次 App 请求:%s", exc.reason)
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/wecom/callback")
    def verify(msg_signature: str, timestamp: str, nonce: str, echostr: str) -> Response:
        """企微后台配置回调地址时的一次性握手。"""
        svc: Services = app.state.services
        if svc.callback is None:
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        try:
            plain = svc.callback.verify_url(
                msg_signature=msg_signature, timestamp=timestamp, nonce=nonce, echostr=echostr
            )
        except CallbackRejected as exc:
            log.warning("回调 URL 验证失败:%s", exc)
            return Response(status_code=status.HTTP_400_BAD_REQUEST)
        return Response(content=plain, media_type="text/plain")

    @app.post("/wecom/callback")
    async def receive(request: Request, msg_signature: str, timestamp: str, nonce: str) -> Response:
        svc: Services = app.state.services
        if svc.callback is None:
            # 没配企微就没有这个入口。回 503 而不是 404 —— 它是"暂时没开",
            # 不是"不存在",配上就有了
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        body = await request.body()

        try:
            message = svc.callback.parse_message(
                body=body, msg_signature=msg_signature, timestamp=timestamp, nonce=nonce
            )
        except CallbackRejected as exc:
            # 只记不回:告诉对方错在哪一步等于帮他调试
            log.warning("回调被拒:%s", exc)
            return Response(status_code=status.HTTP_400_BAD_REQUEST)

        try:
            with session_scope() as session:
                handle_message(
                    session,
                    message=message,
                    deps=QaDeps(
                        llm=svc.llm,
                        channel=svc.channel,
                        resolve_user=svc.resolve_user,
                        gateway_factory=default_gateway,
                    ),
                    now=datetime.now(UTC),
                )
        except Exception:  # noqa: BLE001
            # 企微会对非 200 重投。问答失败重投也不会好,而重投意味着再花一次
            # 模型钱、再回一次消息 —— 所以处理失败照样回 200,失败记在日志里
            log.exception("处理回调消息失败,msg_id=%s", message.msg_id)

        # 企微要求 5 秒内响应。空响应表示"收到了,不用回消息" ——
        # 真正的回复是我们主动 send 出去的,不走这个响应体
        return Response(content="", media_type="text/plain")

    return app


def _start_inboxes(services: Services) -> list:
    """给每个用户起一个微信入站线程,返回它们的停止开关。

    没配微信的线程会立刻自己退出 —— 所以这里不判断配没配,少一处会漂移的判断。
    """
    stops = []
    with session_scope() as session:
        user_ids = users.list_active_users(session)
    for user_id in user_ids:
        _thread, stop = start_inbox_thread(
            user_id, services=services, session_factory=session_scope
        )
        stops.append(stop)
    return stops
