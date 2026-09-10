"""HTTP 入口。

三组端点,**各自的认证面完全分开**(06 §6.1):

| 组 | 谁在调 | 认证 |
| --- | --- | --- |
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

from fastapi import FastAPI, Request, Response, status

from lifein.api import console, enroll, ingest, query
from lifein.api.deps import AuthRejected
from lifein.bootstrap import Services, build_services
from lifein.db import session_scope
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
    app.include_router(console.router)
    app.include_router(enroll.router)
    app.include_router(ingest.router)
    app.include_router(query.router)

    @app.exception_handler(AuthRejected)
    def _auth_rejected(_request: Request, exc: AuthRejected) -> Response:
        # 原因只进日志。响应体是空的 —— 401 之外不给对方任何信息
        log.warning("拒绝一次 App 请求:%s", exc.reason)
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

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
