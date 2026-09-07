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
