"""导出与彻底注销(P4 第 6 片)。需要真实 PostgreSQL。

[09 隐私说明](../docs/09-privacy.md)第 5 节里"要找白杨的"两件事。
这一组盯两件:

1. **凭据不在导出里。** 导出文件会躺在下载目录、会被发到微信,
   而里面如果有邮箱授权码,那份文件的危险程度就超过了它保护的东西
2. **注销是真的删干净。** 删到一半的账号比没删更糟 ——
   它看起来注销了,而数据还在
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from lifein.repos import credentials, export, transactions
from lifein.repos.transactions import Direction, TxnKind
from tests.conftest import api_settings

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def some_data(session, user_id):
    event_id = session.execute(
        text(
            "INSERT INTO raw_events (user_id, source, external_id, occurred_at, trust, raw)"
            " VALUES (:u, 'notification', 'n-1', :t, 'external', '{}'::jsonb) RETURNING id"
        ),
        {"u": user_id, "t": NOW},
    ).scalar_one()
    transactions.record(
        user_id,
        session,
        occurred_at=NOW,
        amount=Decimal("38.50"),
        direction=Direction.DEBIT,
        kind=TxnKind.EXPENSE,
        channel="bank_sms",
        source_event_id=event_id,
        confidence=1.0,
        category="餐饮",
    )
    import base64
    import os

    credentials.put_credential(
        user_id,
        session,
        kind="imap",
        scope="query",
        payload={"secret": base64.b64encode(os.urandom(32)).decode()},
        settings=api_settings(),
    )
    return event_id


class TestExporting:
    def test_it_contains_the_data(self, pg_session, user_id):
        some_data(pg_session, user_id)

        data = export.export_user(user_id, pg_session, now=NOW)

        assert len(data.tables["raw_events"]) == 1
        assert len(data.tables["transactions"]) == 1

    def test_credentials_are_never_exported(self, pg_session, user_id):
        """**这一条是这组测试存在的理由。** 导出文件会躺在下载目录、
        会被发到微信,而凭据在库里是加密的 —— 导出成明文等于把加密作废。"""
        some_data(pg_session, user_id)

        data = export.export_user(user_id, pg_session, now=NOW)

        assert "credentials" not in data.tables
        assert "enrollment_codes" not in data.tables

    def test_money_stays_a_string(self, pg_session, user_id):
        """**JSON 的 number 是双精度浮点。** 38.50 会变成
        38.499999999999996,而这份文件是给人看"我花了多少"的。"""
        some_data(pg_session, user_id)

        (txn,) = export.export_user(user_id, pg_session, now=NOW).tables["transactions"]
        assert txn["amount"] == "38.50"

    def test_it_is_json_serialisable(self, pg_session, user_id):
        """时间、Decimal、二进制都要能落成文本 —— 否则导出到一半才炸,
        而那时你已经告诉人家"在导了"。"""
        import json

        some_data(pg_session, user_id)
        data = export.export_user(user_id, pg_session, now=NOW)

        json.dumps(data.tables, ensure_ascii=False)  # 不抛就算过

    def test_it_does_not_reach_across_users(self, pg_session, user_id):
        """铁律 1。**导出是最不能串的动作之一** —— 串了就是把别人的
        全部数据交到了这个人手上。"""
        some_data(pg_session, user_id)
        other = "99999999-9999-9999-9999-999999999999"

        assert export.export_user(other, pg_session, now=NOW).row_count() == 0


class TestPurging:
    def test_it_deletes_everything_including_credentials(self, pg_session, user_id):
        """**注销和"删掉采集数据"不是一件事。** 后者删的是通知那一路,
        前者删的是账号本身 —— 凭据、待办、记忆、账本,全部。"""
        some_data(pg_session, user_id)

        removed = export.purge_user(user_id, pg_session)

        assert removed["raw_events"] == 1
        assert removed["transactions"] == 1
        assert removed["credentials"] == 1
        assert export.export_user(user_id, pg_session, now=NOW).row_count() == 0

    def test_the_user_row_is_left_for_the_caller(self, pg_session, user_id):
        """**删到一半失败时,留着 `users` 才知道这个账号处理了一半。**"""
        some_data(pg_session, user_id)
        export.purge_user(user_id, pg_session)

        from lifein.repos import users

        assert users.get_user(user_id, pg_session) is not None

    def test_the_order_respects_foreign_keys(self, pg_session, user_id):
        """反了的话数据库直接拒绝,而那个报错完全不解释应该先删哪一个 ——
        和 `data_control.delete_collected` 踩过的是同一个坑。"""
        some_data(pg_session, user_id)
        export.purge_user(user_id, pg_session)  # 不抛就算过

    def test_it_does_not_reach_across_users(self, pg_session, user_id):
        """铁律 1。**删除串了没有任何地方留下痕迹。**"""
        some_data(pg_session, user_id)
        other = "99999999-9999-9999-9999-999999999999"

        assert export.purge_user(other, pg_session) == {}
        assert export.export_user(user_id, pg_session, now=NOW).row_count() > 0


def test_every_exported_table_is_also_purged():
    """**导得出来的东西必须删得掉。** 一张表能导出却删不掉,意味着注销之后
    那份数据还在 —— 而用户拿着导出文件会以为那就是全部,并且以为它已经没了。
    """
    missing = set(export.EXPORTED_TABLES) - set(export.DELETED_TABLES)
    assert missing == set()
