"""测试夹具。

**数据库测试需要真实 PostgreSQL**,不用 SQLite 顶替:这个项目的表结构里
有 JSONB、数组、部分索引和几条决定安全性的 CHECK 约束,SQLite 一个都不支持。
用它测出来的"通过"是假的,比不测更危险。

跑法:

    set TEST_DATABASE_URL=postgresql+psycopg://...@localhost:5432/lifein_test
    pytest -m integration

没设这个变量时整组自动跳过,所以日常 `pytest` 不需要装数据库。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest


def _test_database_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def pg_engine():
    url = _test_database_url()
    if not url:
        pytest.skip("未设置 TEST_DATABASE_URL,跳过需要真实数据库的测试")

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    engine = create_engine(url, future=True)

    # 每次从零建库:迁移本身也是被测对象,复用旧库等于不测它
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    yield engine
    engine.dispose()


@pytest.fixture
def pg_session(pg_engine) -> Iterator:
    """一个事务,测完回滚 —— 用例之间不留痕迹。"""
    from sqlalchemy.orm import Session

    connection = pg_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def user_id(pg_session) -> str:
    """建一个用户。所有表都带 user_id,测试也不例外(铁律 1)。"""
    from sqlalchemy import text

    new_id = str(uuid.uuid4())
    pg_session.execute(
        text("INSERT INTO users (id, display_name, wecom_userid) VALUES (:id, :name, :wecom)"),
        {"id": new_id, "name": "测试用户", "wecom": f"test-{new_id[:8]}"},
    )
    return new_id


# ---------- 接口层的公共夹具 ----------
#
# 放这里而不是某个测试文件里,是因为账本那一组(test_api_ledger)要用同一套。
# 靠 import 从别的测试文件里拿夹具也能跑,但那样每个用例的参数都会被 ruff
# 报成"重定义" —— 而给六十个用例各加一行 noqa,只是把噪音换了个地方。

DEVICE = "pixel-7a"
NOW = datetime(2026, 9, 8, 2, 0, 0, tzinfo=UTC)
"""北京时间 10:00。定住是为了能验时间窗。"""


def api_settings():
    from lifein.config import Settings
    from tests.test_config import BASE

    return Settings(_env_file=None, **BASE)


@pytest.fixture
def settings_factory():
    return api_settings


@pytest.fixture(autouse=True)
def tools_registered():
    """确认待确认那条要过网关,而网关要查得到 agent 的白名单。

    **autouse**:注册是导入即完成的,重复调用不花什么钱,而漏掉一次的表现是
    "这个 agent 的白名单里没有那个工具" —— 一条和权限问题长得一模一样的报错。
    """
    from lifein.bootstrap import register_tools

    register_tools()


@pytest.fixture
def secrets(pg_session, user_id) -> dict[str, str]:
    """给这台设备签发两把密钥 —— 采集一把、查询一把,分开签发(铁律 12)。"""
    import base64
    import os

    from lifein.repos import credentials

    issued = {}
    for kind, scope in ((credentials.INGEST_KIND, "ingest"), (credentials.QUERY_KIND, "query")):
        value = base64.b64encode(os.urandom(32)).decode()
        credentials.put_credential(
            user_id,
            pg_session,
            kind=kind,
            scope=scope,
            payload={"secret": value},
            settings=api_settings(),
            device_id=DEVICE,
        )
        issued[scope] = value
    return issued


@pytest.fixture
def client(pg_session):
    from fastapi.testclient import TestClient

    from lifein.api.app import create_app
    from lifein.api.deps import get_app_settings, get_session, now_utc
    from lifein.bootstrap import Services

    app = create_app(Services(settings=None, llm=None, channel=None, alerter=None))
    app.dependency_overrides[get_session] = lambda: pg_session
    app.dependency_overrides[get_app_settings] = api_settings
    app.dependency_overrides[now_utc] = lambda: NOW
    return TestClient(app)


def signed(client, path, body, *, user_id, secret, when=NOW, device=DEVICE):
    """按 06 §6.2 给请求签名:对**实际发出去的字节**签,不是对对象签。"""
    import base64
    import hashlib
    import hmac
    import json

    from lifein.api import auth

    payload = json.dumps(body).encode()
    timestamp = str(int(when.timestamp()))
    message = auth.signing_string(method="POST", path=path, timestamp=timestamp, body=payload)
    return client.post(
        path,
        content=payload,
        headers={
            "Content-Type": "application/json",
            auth.HEADER_USER: user_id,
            auth.HEADER_DEVICE: device,
            auth.HEADER_TIMESTAMP: timestamp,
            auth.HEADER_SIGNATURE: hmac.new(
                base64.b64decode(secret), message, hashlib.sha256
            ).hexdigest(),
        },
    )


@pytest.fixture
def token(client, user_id, secrets) -> str:
    response = signed(
        client, "/app/token", {"device_id": DEVICE}, user_id=user_id, secret=secrets["query"]
    )
    assert response.status_code == 200
    return response.json()["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
