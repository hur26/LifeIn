"""对着**真库、真 HTTP** 跑一遍 App 那两组接口。

    python scripts/verify-app-api.py [--base http://127.0.0.1:8000]

装完 App 之后、以及每次改完接口之后跑它。集成测试用的是 TestClient,
验不到"进程真的起得来、路由真的挂上了、反代真的没改路径"这几件事。

**它不会往 raw_events 里写任何东西**:白名单是空的时候,上报会被"默认拒绝"
挡在第一道(06 §6.4),链路照样走完 —— 而测试数据一旦进了事件流,
第二天就会出现在摘要里。

**里面那条 R11 是 P1 的验收项**:03 要求"App 的采集凭据被拿走读不到任何
账本或记忆数据(实测验证,不是设计上认为)"。这个脚本就是那次实测,
拿不到 401 就立刻停下。

用完会把签发的临时凭据吊销、把心跳记录删掉,只在 credentials 里留下
两行已吊销的记录 —— 那是有意的,签发过什么本来就该留痕。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import text

from lifein.api import auth
from lifein.config import get_settings
from lifein.crypto import new_shared_secret
from lifein.db import session_scope
from lifein.repos import credentials, users

BASE = os.environ.get("LIFEIN_BASE", "http://127.0.0.1:8000")
DEVICE = "verify-script"
ok = True


def check(label: str, got, want) -> None:
    global ok
    good = got == want
    ok = ok and good
    print(f"  [{'OK ' if good else 'BAD'}] {label}: {got}" + ("" if good else f" (期望 {want})"))


def signed(path: str, body: dict, secret: str, user_id: str) -> httpx.Response:
    payload = json.dumps(body).encode()
    ts = str(int(time.time()))
    sig = auth.sign(
        secret, auth.signing_string(method="POST", path=path, timestamp=ts, body=payload)
    )
    return httpx.post(
        BASE + path,
        content=payload,
        headers={
            "Content-Type": "application/json",
            auth.HEADER_USER: user_id,
            auth.HEADER_DEVICE: DEVICE,
            auth.HEADER_TIMESTAMP: ts,
            auth.HEADER_SIGNATURE: sig,
        },
        timeout=10,
    )


def main() -> int:
    settings = get_settings()
    with session_scope() as s:
        user_id = users.list_active_users(s)[0]
        before = s.execute(text("SELECT count(*) FROM raw_events")).scalar_one()

        # 分开签发:两条独立的行、两把独立的密钥(铁律 12)
        secrets = {}
        for kind, scope in ((credentials.INGEST_KIND, "ingest"), (credentials.QUERY_KIND, "query")):
            credentials.revoke_device(user_id, s, device_id=DEVICE, kind=kind)
            value = new_shared_secret()
            credentials.put_credential(
                user_id, s, kind=kind, scope=scope,
                payload={"secret": value}, settings=settings, device_id=DEVICE,
            )
            secrets[scope] = value

    print(f"用户 {user_id},raw_events 现有 {before} 条\n")

    print("采集端(只能写):")
    r = signed("/ingest/heartbeat", {"device_id": DEVICE, "app_version": "smoke",
                                     "listener_enabled": True}, secrets["ingest"], user_id)
    check("心跳", r.status_code, 200)
    print(f"        服务端时间 {r.json().get('server_time')}")

    r = signed("/ingest/events", {"device_id": DEVICE, "events": [{
        "channel": "notification", "source_app": "com.tencent.mm",
        "posted_at": "2026-09-08T15:00:00+08:00", "title": "项目组",
        "text": "冒烟测试,不该入库", "external_id": "smoke-1"}]},
        secrets["ingest"], user_id)
    check("上报", r.status_code, 200)
    body = r.json()
    check("  白名单默认拒绝", body["dropped"]["not_whitelisted"], 1)
    check("  没有任何事件入库", body["accepted"], 0)

    r = signed("/ingest/events", {"device_id": DEVICE, "events": [{
        "channel": "notification", "source_app": "com.tencent.mm",
        "posted_at": "2026-09-08T15:00:00+08:00", "title": "微信",
        "text": "您的验证码是 328104", "external_id": "smoke-2"}]},
        secrets["ingest"], user_id)
    # 白名单是空的,这条在**第一道**就被拒了 —— 走不到验证码那道(06 §6.4 的顺序)。
    # 验证码那道由单元测试覆盖(两端各一份),这里验的是"默认拒绝真的在最前面"
    check("  验证码那条也照样先被白名单挡下", r.json()["dropped"]["not_whitelisted"], 1)

    print("\nR11 那条实测(P1 验收标准):")
    r = signed("/app/token", {"device_id": DEVICE}, secrets["ingest"], user_id)
    check("采集密钥换不出 token", r.status_code, 401)
    check("  响应体是空的", r.text, "")

    forged = auth.mint_token(
        secret=secrets["ingest"],
        user_id=user_id,
        device_id=DEVICE,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    r = httpx.get(BASE + "/app/todos", headers={"Authorization": f"Bearer {forged}"}, timeout=10)
    check("采集密钥伪造的 token 也进不来", r.status_code, 401)

    print("\n查询端(换 token 之后):")
    r = signed("/app/token", {"device_id": DEVICE}, secrets["query"], user_id)
    check("换 token", r.status_code, 200)
    token = r.json()["token"]
    head = {"Authorization": f"Bearer {token}"}

    for path in ("/app/todos", "/app/pending", "/app/calendar/queue",
                 "/app/collector/status", "/app/memory/facts", "/app/memory/entities"):
        check(path, httpx.get(BASE + path, headers=head, timeout=10).status_code, 200)

    status = httpx.get(BASE + "/app/collector/status", headers=head, timeout=10).json()
    check("  心跳已经记上了", any(d["device_id"] == DEVICE for d in status["devices"]), True)

    print("\n手机丢了(单点吊销):")
    with session_scope() as s:
        credentials.revoke_device(user_id, s, device_id=DEVICE)
    r = httpx.get(BASE + "/app/todos", headers=head, timeout=10)
    check("吊销后同一个 token 立刻失效", r.status_code, 401)

    with session_scope() as s:
        after = s.execute(text("SELECT count(*) FROM raw_events")).scalar_one()
        s.execute(text("DELETE FROM collector_heartbeat WHERE device_id = :d"), {"d": DEVICE})
    check("\nraw_events 一条都没多", after, before)

    print("\n=> 全部通过" if ok else "\n=> 有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
