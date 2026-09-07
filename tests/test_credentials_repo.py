"""凭据仓储的集成测试。

需要真实 PostgreSQL(见 conftest.py)。

这一组回答的是三个问题:库里躺的是不是密文、scope 隔离是不是真的、
密钥轮换能不能收尾。前两个是安全属性,第三个是运维属性 —— 没有第三个,
前两个迟早会因为"不敢换密钥"而失效。
"""

from __future__ import annotations

import base64
import os

import pytest
from sqlalchemy import text

from lifein.config import Settings
from lifein.crypto import DecryptError
from lifein.repos.credentials import (
    get_credential,
    put_credential,
    revoke_credential,
    rotate_credentials,
    stale_key_versions,
)
from tests.test_config import BASE

pytestmark = pytest.mark.integration

IMAP_PAYLOAD = {"host": "imap.163.com", "username": "me@163.com", "auth_code": "16位授权码"}


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**BASE, **overrides})


def key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def test_ciphertext_in_the_database_is_not_plaintext(pg_session, user_id):
    s = settings()
    put_credential(
        user_id, pg_session, kind="imap", scope="query", payload=IMAP_PAYLOAD, settings=s
    )
    blob = pg_session.execute(text("SELECT ciphertext FROM credentials")).scalar_one()

    assert "授权码".encode() not in bytes(blob)
    assert b"imap.163.com" not in bytes(blob)


def test_roundtrip(pg_session, user_id):
    s = settings()
    put_credential(
        user_id, pg_session, kind="imap", scope="query", payload=IMAP_PAYLOAD, settings=s
    )
    assert get_credential(user_id, pg_session, kind="imap", settings=s) == IMAP_PAYLOAD


def test_missing_credential_is_none_not_an_error(pg_session, user_id):
    assert get_credential(user_id, pg_session, kind="imap", settings=settings()) is None


class TestScopeIsolation:
    """R11:采集凭据打到读接口一律拒绝,不做"反正同一个用户"的通融。"""

    def test_ingest_credential_is_invisible_to_query_reads(self, pg_session, user_id):
        s = settings()
        put_credential(
            user_id,
            pg_session,
            kind="collector",
            scope="ingest",
            payload={"device_key": "k"},
            settings=s,
        )
        assert get_credential(user_id, pg_session, kind="collector", settings=s) is None

    def test_ingest_read_finds_it(self, pg_session, user_id):
        s = settings()
        put_credential(
            user_id,
            pg_session,
            kind="collector",
            scope="ingest",
            payload={"device_key": "k"},
            settings=s,
        )
        got = get_credential(user_id, pg_session, kind="collector", settings=s, for_ingest=True)
        assert got == {"device_key": "k"}

    def test_both_scope_works_either_way(self, pg_session, user_id):
        s = settings()
        put_credential(
            user_id, pg_session, kind="wecom", scope="both", payload={"a": 1}, settings=s
        )
        assert get_credential(user_id, pg_session, kind="wecom", settings=s) == {"a": 1}
        assert get_credential(user_id, pg_session, kind="wecom", settings=s, for_ingest=True) == {
            "a": 1
        }

    def test_bogus_scope_is_rejected(self, pg_session, user_id):
        with pytest.raises(ValueError):
            put_credential(
                user_id,
                pg_session,
                kind="imap",
                scope="随便",
                payload={},
                settings=settings(),
            )


def test_another_user_cannot_read_it(pg_session, user_id):
    s = settings()
    put_credential(
        user_id, pg_session, kind="imap", scope="query", payload=IMAP_PAYLOAD, settings=s
    )
    other = "99999999-9999-9999-9999-999999999999"
    assert get_credential(other, pg_session, kind="imap", settings=s) is None


def test_revoked_credential_is_invisible_but_still_on_record(pg_session, user_id):
    """吊销不删除:哪条凭据什么时候被吊销的,是排查手机丢失后唯一的线索。"""
    s = settings()
    put_credential(
        user_id, pg_session, kind="imap", scope="query", payload=IMAP_PAYLOAD, settings=s
    )
    assert revoke_credential(user_id, pg_session, kind="imap") == 1
    assert get_credential(user_id, pg_session, kind="imap", settings=s) is None
    assert pg_session.execute(text("SELECT count(*) FROM credentials")).scalar_one() == 1


class TestRotation:
    def test_stale_versions_are_listed(self, pg_session, user_id):
        old = settings(master_key=key(), master_key_version=1)
        put_credential(
            user_id, pg_session, kind="imap", scope="query", payload=IMAP_PAYLOAD, settings=old
        )

        new = settings(
            master_key=key(),
            master_key_version=2,
            master_key_previous=old.master_key.get_secret_value(),
        )
        stale = stale_key_versions(user_id, pg_session, settings=new)
        assert [k for _id, k, _v in stale] == ["imap"]

    def test_rotation_rewrites_and_clears_the_list(self, pg_session, user_id):
        old = settings(master_key=key(), master_key_version=1)
        put_credential(
            user_id, pg_session, kind="imap", scope="query", payload=IMAP_PAYLOAD, settings=old
        )
        new = settings(
            master_key=key(),
            master_key_version=2,
            master_key_previous=old.master_key.get_secret_value(),
        )

        assert rotate_credentials(user_id, pg_session, settings=new) == 1
        assert stale_key_versions(user_id, pg_session, settings=new) == []

        # 换完之后,只有新密钥能读 —— 旧密钥彻底可以删了
        only_new = settings(master_key=new.master_key.get_secret_value(), master_key_version=2)
        assert get_credential(user_id, pg_session, kind="imap", settings=only_new) == IMAP_PAYLOAD

    def test_decryption_failure_is_raised_not_swallowed(self, pg_session, user_id):
        """解不开比没有更严重 —— 返回 None 会让调用方以为"还没配过"。"""
        old = settings(master_key=key(), master_key_version=1)
        put_credential(
            user_id, pg_session, kind="imap", scope="query", payload=IMAP_PAYLOAD, settings=old
        )
        wrong = settings(master_key=key(), master_key_version=1)  # 同版本号,不同密钥
        with pytest.raises(DecryptError):
            get_credential(user_id, pg_session, kind="imap", settings=wrong)
