"""设备凭据的集成测试 —— 采集与查询分开签发、按设备吊销(06 §6.1)。

需要真实 PostgreSQL(见 conftest.py)。

这一组盯的是[铁律 12](../AGENTS.md) 和 [R11](../docs/05-risks.md) 的那句
"最重要的一条":**采集端只能写不能读**。它不是靠路由挂对了依赖保证的,
而是靠仓储层的 scope 过滤 —— 所以这里直接在仓储层验它,
绕过所有 HTTP 的东西。
"""

from __future__ import annotations

import base64
import os

import pytest

from lifein.config import Settings
from lifein.repos.credentials import (
    INGEST_KIND,
    QUERY_KIND,
    get_device_credential,
    list_device_credentials,
    put_credential,
    revoke_device,
)
from tests.test_config import BASE

pytestmark = pytest.mark.integration

PHONE = "pixel-7a"
OTHER_PHONE = "old-mi9"


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**BASE, **overrides})


def secret() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def issue(session, user_id, *, device_id: str, kind: str, scope: str, s: Settings) -> str:
    value = secret()
    put_credential(
        user_id,
        session,
        kind=kind,
        scope=scope,
        payload={"secret": value},
        settings=s,
        device_id=device_id,
    )
    return value


def test_ingest_credential_cannot_be_read_as_query(pg_session, user_id):
    """P1 验收标准那条"采集凭据被拿走读不到任何账本" —— 实测,不是设计上认为。"""
    s = settings()
    issue(pg_session, user_id, device_id=PHONE, kind=INGEST_KIND, scope="ingest", s=s)

    # 采集那条,用查询的口子取:取不到
    assert (
        get_device_credential(
            user_id, pg_session, kind=INGEST_KIND, device_id=PHONE, settings=s
        )
        is None
    )
    # 反过来也一样:还没签发查询凭据,采集密钥不会顶上
    assert (
        get_device_credential(
            user_id, pg_session, kind=QUERY_KIND, device_id=PHONE, settings=s, for_ingest=False
        )
        is None
    )


def test_two_credentials_on_one_device_are_independent(pg_session, user_id):
    s = settings()
    collect = issue(pg_session, user_id, device_id=PHONE, kind=INGEST_KIND, scope="ingest", s=s)
    query = issue(pg_session, user_id, device_id=PHONE, kind=QUERY_KIND, scope="query", s=s)

    assert collect != query  # 分开签发,不是同一把
    assert get_device_credential(
        user_id, pg_session, kind=INGEST_KIND, device_id=PHONE, settings=s, for_ingest=True
    ) == {"secret": collect}
    assert get_device_credential(
        user_id, pg_session, kind=QUERY_KIND, device_id=PHONE, settings=s
    ) == {"secret": query}


def test_revoking_one_device_leaves_the_others_alone(pg_session, user_id):
    """手机丢了要能单点吊销,而不是把所有设备一起断掉。"""
    s = settings()
    issue(pg_session, user_id, device_id=PHONE, kind=INGEST_KIND, scope="ingest", s=s)
    issue(pg_session, user_id, device_id=PHONE, kind=QUERY_KIND, scope="query", s=s)
    issue(pg_session, user_id, device_id=OTHER_PHONE, kind=QUERY_KIND, scope="query", s=s)

    # 不给 kind = 这台设备的全部凭据一起吊销
    assert revoke_device(user_id, pg_session, device_id=PHONE) == 2

    assert (
        get_device_credential(
            user_id, pg_session, kind=INGEST_KIND, device_id=PHONE, settings=s, for_ingest=True
        )
        is None
    )
    assert (
        get_device_credential(user_id, pg_session, kind=QUERY_KIND, device_id=PHONE, settings=s)
        is None
    )
    assert get_device_credential(
        user_id, pg_session, kind=QUERY_KIND, device_id=OTHER_PHONE, settings=s
    ) is not None


def test_revoking_one_kind_keeps_the_other(pg_session, user_id):
    """只停采集、留着查询 —— 采集器出问题时的处置方式。"""
    s = settings()
    issue(pg_session, user_id, device_id=PHONE, kind=INGEST_KIND, scope="ingest", s=s)
    issue(pg_session, user_id, device_id=PHONE, kind=QUERY_KIND, scope="query", s=s)

    assert revoke_device(user_id, pg_session, device_id=PHONE, kind=INGEST_KIND) == 1

    assert (
        get_device_credential(
            user_id, pg_session, kind=INGEST_KIND, device_id=PHONE, settings=s, for_ingest=True
        )
        is None
    )
    assert get_device_credential(
        user_id, pg_session, kind=QUERY_KIND, device_id=PHONE, settings=s
    ) is not None


def test_reissuing_takes_the_old_secret_out_of_service(pg_session, user_id):
    """重新签发时旧的必须失效。

    留着两把有效的,验签用哪把取决于排序 —— 而"换了密钥但旧的还能用"
    是最难发现的一类问题。
    """
    s = settings()
    old = issue(pg_session, user_id, device_id=PHONE, kind=QUERY_KIND, scope="query", s=s)
    revoke_device(user_id, pg_session, device_id=PHONE, kind=QUERY_KIND)
    new = issue(pg_session, user_id, device_id=PHONE, kind=QUERY_KIND, scope="query", s=s)

    stored = get_device_credential(
        user_id, pg_session, kind=QUERY_KIND, device_id=PHONE, settings=s
    )
    assert stored == {"secret": new}
    assert stored != {"secret": old}


def test_listing_keeps_revoked_rows(pg_session, user_id):
    """已吊销的照样列出来:签发过什么、什么时候断的,列表里没有就回答不了。"""
    s = settings()
    issue(pg_session, user_id, device_id=PHONE, kind=INGEST_KIND, scope="ingest", s=s)
    revoke_device(user_id, pg_session, device_id=PHONE)

    rows = list_device_credentials(user_id, pg_session)
    assert [(r.device_id, r.kind, r.active) for r in rows] == [(PHONE, INGEST_KIND, False)]
    assert rows[0].revoked_at is not None
