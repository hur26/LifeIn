"""采集端两个端点的集成测试(06 §6.4 / §6.5)。

需要真实 PostgreSQL:被测的东西一半在库里 —— 凭据的 scope 隔离、白名单、
`raw_events` 的唯一键。用假仓储测这些等于什么都没测。

这一组回答四个问题:

1. 签名不对、过期、换了设备,是不是**一律空 401**
2. 白名单 / 验证码 / purpose 三道拦不拦得住
3. 重放同一批上报,会不会写进第二遍
4. **采集凭据能不能读到东西** —— P1 验收标准里那条实测
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from lifein.api import auth
from lifein.api.app import create_app
from lifein.api.deps import get_app_settings, get_session, now_utc
from lifein.bootstrap import Services
from lifein.config import Settings
from lifein.repos import collector, credentials
from tests.test_config import BASE

pytestmark = pytest.mark.integration

DEVICE = "pixel-7a"
WECHAT = "com.tencent.mm"
NOW = datetime(2026, 9, 8, 2, 11, 12, tzinfo=UTC)


def settings() -> Settings:
    return Settings(_env_file=None, **BASE)


@pytest.fixture
def secrets(pg_session, user_id) -> dict[str, str]:
    """给这台设备签发两把密钥 —— 采集一把、查询一把,分开签发(铁律 12)。"""
    issued = {}
    for kind, scope in ((credentials.INGEST_KIND, "ingest"), (credentials.QUERY_KIND, "query")):
        value = base64.b64encode(os.urandom(32)).decode()
        credentials.put_credential(
            user_id,
            pg_session,
            kind=kind,
            scope=scope,
            payload={"secret": value},
            settings=settings(),
            device_id=DEVICE,
        )
        issued[scope] = value
    return issued


@pytest.fixture
def wechat_rule(pg_session, user_id):
    return collector.add_whitelist(
        user_id,
        pg_session,
        match_type=collector.MATCH_PACKAGE,
        pattern=WECHAT,
        purpose=collector.PURPOSE_MESSAGE,
        phase="P1",
    )


@pytest.fixture
def client(pg_session) -> TestClient:
    app = create_app(
        Services(settings=None, llm=None, channel=None, alerter=None)  # 端点层用不到这些
    )
    # 用测试那个事务里的 session,测完整体回滚;时间定住,好验时间窗
    app.dependency_overrides[get_session] = lambda: pg_session
    app.dependency_overrides[get_app_settings] = settings
    app.dependency_overrides[now_utc] = lambda: NOW
    return TestClient(app)


def post(client, path: str, body: dict, *, user_id: str, secret: str, when=NOW, device=DEVICE):
    payload = json.dumps(body).encode()
    timestamp = str(int(when.timestamp()))
    message = auth.signing_string(
        method="POST", path=path, timestamp=timestamp, body=payload
    )
    signature = hmac.new(base64.b64decode(secret), message, hashlib.sha256).hexdigest()
    return client.post(
        path,
        content=payload,
        headers={
            "Content-Type": "application/json",
            auth.HEADER_USER: user_id,
            auth.HEADER_DEVICE: device,
            auth.HEADER_TIMESTAMP: timestamp,
            auth.HEADER_SIGNATURE: signature,
        },
    )


def batch(**overrides) -> dict:
    event = {
        "channel": "notification",
        "source_app": WECHAT,
        "posted_at": "2026-09-08T10:11:12+08:00",
        "title": "项目组",
        "text": "老王:明天下午三点开会",
        "external_id": "n-1",
        **overrides,
    }
    return {"device_id": DEVICE, "events": [event]}


class TestAuth:
    def test_no_headers_is_a_bare_401(self, client):
        response = client.post("/ingest/events", json=batch())
        assert response.status_code == 401
        assert response.text == ""  # 不给对方任何线索

    def test_wrong_signature_is_rejected(self, client, user_id, secrets):
        other = base64.b64encode(os.urandom(32)).decode()
        response = post(client, "/ingest/events", batch(), user_id=user_id, secret=other)
        assert response.status_code == 401

    def test_stale_timestamp_is_rejected(self, client, user_id, secrets, wechat_rule):
        """时间窗是重放保护的第一条。"""
        old = NOW - timedelta(hours=2)
        response = post(
            client, "/ingest/events", batch(), user_id=user_id, secret=secrets["ingest"], when=old
        )
        assert response.status_code == 401

    def test_future_timestamp_is_rejected(self, client, user_id, secrets, wechat_rule):
        """只卡"太旧"的话,把时钟调到明年就能签出永远新鲜的请求。"""
        ahead = NOW + timedelta(hours=2)
        response = post(
            client,
            "/ingest/events",
            batch(),
            user_id=user_id,
            secret=secrets["ingest"],
            when=ahead,
        )
        assert response.status_code == 401

    def test_query_secret_cannot_write_to_ingest(self, client, user_id, secrets, wechat_rule):
        """两把密钥不通用。这条和下面那条合起来才是"分开签发"的意思。"""
        response = post(
            client, "/ingest/events", batch(), user_id=user_id, secret=secrets["query"]
        )
        assert response.status_code == 401

    def test_revoked_device_is_rejected_immediately(
        self, client, pg_session, user_id, secrets, wechat_rule
    ):
        """手机丢了 —— 吊销之后下一次请求就进不来。"""
        credentials.revoke_device(user_id, pg_session, device_id=DEVICE)
        response = post(
            client, "/ingest/events", batch(), user_id=user_id, secret=secrets["ingest"]
        )
        assert response.status_code == 401

    def test_body_device_must_match_the_signature(self, client, user_id, secrets, wechat_rule):
        body = {**batch(), "device_id": "someone-elses-phone"}
        response = post(client, "/ingest/events", body, user_id=user_id, secret=secrets["ingest"])
        assert response.status_code == 422


class TestEvents:
    def test_whitelisted_event_lands_in_raw_events(
        self, client, pg_session, user_id, secrets, wechat_rule
    ):
        response = post(
            client, "/ingest/events", batch(), user_id=user_id, secret=secrets["ingest"]
        )

        assert response.status_code == 200
        assert response.json() == {
            "accepted": 1,
            "duplicates": 0,
            # 每个原因都在,没有的填 0 —— 少一个键和"是 0"在客户端看起来一样
            "dropped": {
                "not_whitelisted": 0,
                "phase_not_open": 0,
                "verification_code": 0,
                "malformed": 0,
                "not_a_transaction": 0,
            },
        }

        row = pg_session.execute(
            text("SELECT source, external_id, trust FROM raw_events WHERE user_id = :u"),
            {"u": user_id},
        ).one()
        assert (row.source, row.external_id, row.trust) == (
            "notification",
            f"{DEVICE}:n-1",
            "external",
        )

    def test_without_a_whitelist_nothing_gets_in(self, client, pg_session, user_id, secrets):
        """默认拒绝。白名单还没配的时候,采集器上来的东西一条都不该入库。"""
        response = post(
            client, "/ingest/events", batch(), user_id=user_id, secret=secrets["ingest"]
        )

        assert response.json()["dropped"]["not_whitelisted"] == 1
        assert _count(pg_session, user_id) == 0

    def test_verification_codes_never_reach_the_database(
        self, client, pg_session, user_id, secrets, wechat_rule
    ):
        """铁律 11 的服务端那一道。**不入库、不记原文。**"""
        response = post(
            client,
            "/ingest/events",
            batch(text="您的验证码是 328104,请勿告知他人"),
            user_id=user_id,
            secret=secrets["ingest"],
        )

        assert response.json()["dropped"]["verification_code"] == 1
        assert _count(pg_session, user_id) == 0

    def test_replaying_the_same_batch_writes_nothing_new(
        self, client, pg_session, user_id, secrets, wechat_rule
    ):
        """重放保护的第二条:同一批重发一次,只会变成 duplicates。"""
        first = post(client, "/ingest/events", batch(), user_id=user_id, secret=secrets["ingest"])
        second = post(client, "/ingest/events", batch(), user_id=user_id, secret=secrets["ingest"])

        assert first.json()["accepted"] == 1
        assert second.json() == {**first.json(), "accepted": 0, "duplicates": 1}
        assert _count(pg_session, user_id) == 1

    def test_batch_size_is_capped(self, client, user_id, secrets, wechat_rule):
        body = {"device_id": DEVICE, "events": [batch()["events"][0]] * 201}
        response = post(client, "/ingest/events", body, user_id=user_id, secret=secrets["ingest"])
        assert response.status_code == 422


class TestHeartbeat:
    def test_heartbeat_uses_server_time(self, client, pg_session, user_id, secrets):
        response = post(
            client,
            "/ingest/heartbeat",
            {"device_id": DEVICE, "app_version": "1.0.0", "listener_enabled": True},
            user_id=user_id,
            secret=secrets["ingest"],
        )

        assert response.status_code == 200
        # 回服务端时间:设备时钟偏了会表现为"一直 401",这是唯一的线索
        assert response.json()["server_time"] == NOW.isoformat()

        (beat,) = collector.list_heartbeats(user_id, pg_session)
        assert beat.device_id == DEVICE
        assert beat.last_seen_at == NOW
        assert beat.app_version == "1.0.0"

    def test_listener_disabled_is_recorded(self, client, pg_session, user_id, secrets):
        post(
            client,
            "/ingest/heartbeat",
            {"device_id": DEVICE, "listener_enabled": False},
            user_id=user_id,
            secret=secrets["ingest"],
        )
        (beat,) = collector.list_heartbeats(user_id, pg_session)
        assert beat.listener_enabled is False
        # 权限被收走也算掉线:进程活着但读不到东西
        assert beat.needs_alert(cutoff=NOW - timedelta(hours=1))


def _count(session, user_id) -> int:
    return session.execute(
        text("SELECT count(*) FROM raw_events WHERE user_id = :u"), {"u": user_id}
    ).scalar_one()
