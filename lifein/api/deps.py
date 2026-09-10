"""两组接口各自的认证依赖 —— [铁律 12](../../AGENTS.md#1-铁律) 在代码结构上的样子。

06 §6.1 那句"**分离是结构性的,不是约定**"落在这个模块:

- `require_ingest_device` 只认 `kind=collector` + `scope=ingest` 的凭据
- `require_query_device` / `require_app_token` 只认 `kind=app_device` + `scope=query`

两个函数各取各的,没有一条代码路径能从其中一个拿到另一个的凭据。
仓储层的 scope 过滤是最后一道 —— **就算将来有人把依赖挂错了**,
采集密钥也解不出查询凭据那一行(`tests/test_device_credentials.py` 盯着这一条)。

**session 由依赖给出,认证和业务共用同一个。** FastAPI 对同一个请求里的
依赖只解析一次,所以处理函数拿到的就是认证时用的那个 session,
一个请求一个事务 —— 06 §2.7 那条"确认与写入必须同一个事务"才成立。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from lifein.api import auth
from lifein.config import Settings, get_settings
from lifein.db import session_scope
from lifein.repos import credentials

log = logging.getLogger(__name__)


class AuthRejected(Exception):
    """认证没过。`create_app` 把它变成一个**空的 401**。

    带着原因抛,是给日志的;返给对方的响应里一个字都没有。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Caller:
    """认出来是谁。**处理函数只该拿到这个**,不该拿到密钥本身。"""

    user_id: str
    device_id: str


def get_session() -> Iterator[Session]:
    """一个请求一个事务。测试里用 `dependency_overrides` 换成事务内的 session。"""
    with session_scope() as session:
        yield session


def get_app_settings() -> Settings:
    return get_settings()


async def form_values(request: Request) -> dict[str, str]:
    """读一张 HTML 表单。**自己解,不装 `python-multipart`。**

    `request.form()` 要那个包,而装一个新依赖要先补一条 ADR(AGENTS.md §3)——
    那条 ADR 的理由会是"少写六行",而控制台上的表单全是
    `application/x-www-form-urlencoded`(`<form>` 的默认值),
    它就是一串 `a=1&b=2`。

    **不接受 multipart。** 控制台上没有任何一处要传文件,
    而一个能收 multipart 的端点是一处能被喂进任意大的 body 的地方。
    """
    if "multipart/form-data" in request.headers.get("content-type", ""):
        return {}
    raw = await request.body()
    return dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))


def now_utc() -> datetime:
    """当前时间做成依赖,测试里能定住 —— 时间偏移和过期都要按它判。"""
    return datetime.now(UTC)


async def require_ingest_device(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    now: Annotated[datetime, Depends(now_utc)],
) -> Caller:
    """采集端:每次请求验签(06 §6.2)。**只能写**。"""
    return await _verify_signed(
        request,
        session=session,
        settings=settings,
        now=now,
        kind=credentials.INGEST_KIND,
        for_ingest=True,
    )


async def require_query_device(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    now: Annotated[datetime, Depends(now_utc)],
) -> Caller:
    """查询端换 token 那一下:用长期设备凭据验签(06 §6.3)。

    **只有 `/app/token` 走这条。** 其余查询接口走 `require_app_token` ——
    长期密钥每次请求都上网,等于把它暴露 N 倍。
    """
    return await _verify_signed(
        request,
        session=session,
        settings=settings,
        now=now,
        kind=credentials.QUERY_KIND,
        for_ingest=False,
    )


def require_app_token(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    now: Annotated[datetime, Depends(now_utc)],
) -> Caller:
    """查询端:`Authorization: Bearer <token>`。

    验签要**回库取那台设备的凭据**,所以吊销立刻生效 —— 这是 R11 要的
    "token 服务端可单点吊销",代价是每次请求多一次单行查询(06 §6.3)。
    """
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthRejected("没有 Bearer token")

    payload = auth.parse_token(token)  # 还没验签,只是为了知道该拿哪把密钥
    secret = _load_secret(
        session,
        settings=settings,
        user_id=payload.user_id,
        device_id=payload.device_id,
        kind=credentials.QUERY_KIND,
        for_ingest=False,
    )
    try:
        auth.verify_token(token, secret=secret, now=now)
    except auth.AuthError as exc:
        raise AuthRejected(str(exc)) from exc

    return Caller(user_id=payload.user_id, device_id=payload.device_id)


async def _verify_signed(
    request: Request,
    *,
    session: Session,
    settings: Settings,
    now: datetime,
    kind: str,
    for_ingest: bool,
) -> Caller:
    """签名校验。

    这个函数是 `async` 的,只因为要 `await request.body()` —— 而它里面那次
    取凭据是阻塞的库调用,严格说会占住事件循环几毫秒。

    **知道且接受**:本项目的 QPS 是个位数,瓶颈从头到尾在 LLM 侧
    ([ADR-016](../../docs/04-tech-decisions.md#adr-016--服务端用-python--fastapi))。
    真要改的时候,改法是把取凭据挪进线程池,不是把整条链路改成异步 ——
    后者会把 SQLAlchemy 那一层一起拖进来。
    """
    user_id = request.headers.get(auth.HEADER_USER, "").strip()
    device_id = request.headers.get(auth.HEADER_DEVICE, "").strip()
    timestamp = request.headers.get(auth.HEADER_TIMESTAMP, "").strip()
    signature = request.headers.get(auth.HEADER_SIGNATURE, "").strip()
    if not (user_id and device_id and timestamp and signature):
        raise AuthRejected("签名头不全")

    if not auth.timestamp_is_fresh(timestamp, now=now, max_skew_s=settings.ingest_max_skew_s):
        # 时间窗是重放保护的第一条。设备时钟不对时表现为"一直 401",
        # 所以心跳的响应里回了一个服务端时间,好让 App 自己看出来
        raise AuthRejected("时间戳超出允许偏移")

    secret = _load_secret(
        session,
        settings=settings,
        user_id=user_id,
        device_id=device_id,
        kind=kind,
        for_ingest=for_ingest,
    )

    # body 读一次就被 Starlette 缓存,处理函数照样解得出来
    body = await request.body()
    message = auth.signing_string(
        method=request.method, path=request.url.path, timestamp=timestamp, body=body
    )
    if not auth.verify(secret, message, signature):
        raise AuthRejected("签名不对")

    return Caller(user_id=user_id, device_id=device_id)


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
NowDep = Annotated[datetime, Depends(now_utc)]

IngestCaller = Annotated[Caller, Depends(require_ingest_device)]
"""采集端认出来的设备。**挂了这个的路由只能写。**"""

QueryDevice = Annotated[Caller, Depends(require_query_device)]
"""用长期查询凭据验签认出来的设备。只有换 token 那一个端点用。"""

AppCaller = Annotated[Caller, Depends(require_app_token)]
"""查询端认出来的设备。挂了这个的路由才读得到数据。

用别名而不是在每个签名里写 `Depends(...)`,是为了让**这条路由属于哪一组**
一眼可见 —— 挂错组是这套分离唯一可能失效的方式。
"""


def _load_secret(
    session: Session,
    *,
    settings: Settings,
    user_id: str,
    device_id: str,
    kind: str,
    for_ingest: bool,
) -> str:
    """取这台设备的密钥。取不到、被吊销、scope 不符,都是同一个拒绝。"""
    try:
        stored = credentials.get_device_credential(
            user_id,
            session,
            kind=kind,
            device_id=device_id,
            settings=settings,
            for_ingest=for_ingest,
        )
    except Exception as exc:  # noqa: BLE001
        # 解密失败要能查(多半是换了主密钥没重加密),但对外还是那个空 401
        log.exception("取设备凭据失败:user=%s device=%s kind=%s", user_id, device_id, kind)
        raise AuthRejected(f"凭据不可用:{type(exc).__name__}") from exc

    secret = (stored or {}).get("secret", "")
    if not secret:
        raise AuthRejected(f"没有可用的 {kind} 凭据")
    return secret
