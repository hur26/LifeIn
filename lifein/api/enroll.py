"""配码换取(06 §6.15,P4 第 1 片)。

**这是这个系统里唯一一个不需要凭据的写入口** —— 因为它换的就是凭据。
所以它和别的路由不一样,几件事要单独交代:

## 失败一律空 401,三种原因不区分

码不对、用过了、过期了,对调用方来说都是"这个码不能用"。区分等于告诉对方
"这个码存在过",而那是爆破时唯一有用的信息。

## 防爆破靠码的长度,不靠限流

码是 128 位随机数(`enrollment.CODE_BYTES`),穷举不现实。限流要存计数状态,
那又是一张表和一份要维护的清理逻辑,**换来的是对一个已经穷举不动的东西
再加一道**。

**但失败要记进日志。** 连续的失败是"有人在扫这个接口"唯一的信号。

## 密钥只在这一次响应里出现

和 `admin issue-device` 一样:库里存的是密文,服务端自己也读不出来给你看第二遍。
换完之后 App 立刻把它们写进 Keystore(`Secrets.kt`),响应本身不落任何日志。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from lifein.api.deps import NowDep, SessionDep, SettingsDep
from lifein.crypto import new_shared_secret
from lifein.repos import credentials, enrollment, users

log = logging.getLogger(__name__)

router = APIRouter(prefix="/enroll", tags=["enroll"])

MAX_DEVICE_ID = 64


class ClaimIn(BaseModel):
    code: str = Field(min_length=8, max_length=128)
    device_id: str = Field(min_length=4, max_length=MAX_DEVICE_ID)
    """**App 自己生成的**,不再由人现编一个名字(03 的 P4)。

    人编的名字会重复(两个人都叫 `phone`),而重复的 `device_id` 意味着
    **吊销一台会连带吊销另一台** —— 而那时被吊销的那个人不知道发生了什么。
    """

    app_version: str | None = Field(default=None, max_length=32)


@router.post("/claim")
def claim(body: ClaimIn, session: SessionDep, settings: SettingsDep, now: NowDep) -> Response:
    """用一次性码换两把密钥。**换过一次就作废。**"""
    code = enrollment.claim(
        session, code=body.code, device_id=body.device_id, now=now
    )
    if code is None:
        # 三种原因不区分,响应体是空的 —— 和 06 §6.16 那条 401 同一条规矩
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    user = users.get_user(code.user_id, session)
    if user is None or not user.active:
        # 码是有效的,但用户没了或被停用了。**码已经作废掉了**,这是对的:
        # 一张指向不存在用户的码不该还能再试一次
        log.warning("配码指向的用户不可用:%s", code.user_id)
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    issued = _issue_secrets(
        code.user_id, session, settings=settings, device_id=body.device_id, purpose=code.purpose
    )
    log.info(
        "配码完成:user=%s device=%s version=%s",
        code.user_id,
        body.device_id,
        body.app_version,
    )

    return _json(
        {
            "user_id": code.user_id,
            "device_id": body.device_id,
            "base_url": code.base_url,
            **issued,
        }
    )


def _issue_secrets(
    user_id: str, session, *, settings, device_id: str, purpose: str
) -> dict[str, str]:
    """签发密钥。**采集与查询是两条独立的行、两把独立的密钥**(铁律 12)。

    先吊销这台设备同类的旧凭据:留着两把有效的,验签用哪把取决于排序,
    而"换了密钥但旧的还能用"是最难发现的一类问题(和 `issue-device` 同一条)。
    """
    wanted = {
        "all": (("collector", "ingest"), ("app_device", "query")),
        "collect": (("collector", "ingest"),),
        "query": (("app_device", "query"),),
    }[purpose]

    issued: dict[str, str] = {}
    for kind, scope in wanted:
        credentials.revoke_device(user_id, session, device_id=device_id, kind=kind)
        secret = new_shared_secret()
        credentials.put_credential(
            user_id,
            session,
            kind=kind,
            scope=scope,
            payload={"secret": secret},
            settings=settings,
            device_id=device_id,
        )
        issued["collector_secret" if scope == "ingest" else "query_secret"] = secret
    return issued


def _json(payload: dict) -> Response:
    """自己序列化。**这条响应不该被任何中间件顺手记下来** ——
    里面是明文密钥,而 FastAPI 的默认路径上多一层就多一处可能打日志的地方。
    """
    import json

    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        media_type="application/json",
    )
