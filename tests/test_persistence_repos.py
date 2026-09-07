"""审计与推送日志的集成测试。

需要真实 PostgreSQL(见 conftest.py)。

审计这一组盯的是一件容易做反的事:**审计写失败不该让业务调用跟着失败**。
审计记的是"已经发生过的事",此刻抛异常不会让那件事没发生,只会让一次成功
的工具调用看起来像失败,进而触发重试 —— 而重试会把副作用做第二遍。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from lifein.channels.base import Card, CardSection
from lifein.governance.audit import ToolCallRecord
from lifein.governance.registry import ToolLevel
from lifein.repos.push_log import (
    count_active_pushes_since,
    digest_card,
    record_push,
)
from lifein.repos.tool_calls import PostgresAuditSink, record_tool_call

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)


def card() -> Card:
    return Card(
        title="9 月 7 日摘要",
        summary="今天有 2 件要紧事。",
        sections=[CardSection(heading="要做的事", lines=["交房租", "回邮件"])],
    )


def entry(**overrides) -> ToolCallRecord:
    base = dict(
        user_id="ignored",  # 真正用的是 record_tool_call 的第一个参数
        agent="daily_digest",
        tool_name="llm.chat",
        level=ToolLevel.L1,
        args_digest={"messages": {"type": "list", "len": 2}},
        result_status="allowed",
    )
    return ToolCallRecord(**{**base, **overrides})


class TestAudit:
    def test_record_is_persisted(self, pg_session, user_id):
        record_tool_call(
            user_id,
            pg_session,
            entry(
                llm_fields_sent=["body", "标题"],
                prompt_tokens=1000,
                completion_tokens=200,
                cost_cny=Decimal("0.0060"),
                duration_ms=1234,
            ),
        )
        row = pg_session.execute(text("SELECT * FROM tool_calls")).one()
        assert row.tool_name == "llm.chat"
        assert row.llm_fields_sent == ["body", "标题"]  # R12:发出去的字段名可查
        assert row.cost_cny == Decimal("0.0060")

    def test_l2_without_rollback_does_not_blow_up_the_caller(self, pg_session, user_id):
        """约束会挡下它,但审计不该把这次失败变成业务失败。"""
        record_tool_call(user_id, pg_session, entry(level=ToolLevel.L2))
        # 没有抛异常就是这条用例的全部主张
        pg_session.rollback()

    def test_sink_writes_in_the_caller_transaction(self, pg_session, user_id):
        # 审计要和业务写入同一个事务,否则会出现"业务回滚了,审计说做过"
        PostgresAuditSink(user_id, pg_session).record(entry())
        assert pg_session.execute(text("SELECT count(*) FROM tool_calls")).scalar_one() == 1


class TestPushLog:
    def test_digest_keeps_the_title_but_not_the_body(self):
        # 标题本来就是给人看的一行,正文只留长度
        d = digest_card(card())
        assert d["title"] == "9 月 7 日摘要"
        assert "交房租" not in str(d)
        assert d["sections"][0]["lines"] == 2

    def test_active_push_is_counted(self, pg_session, user_id):
        record_push(
            user_id, pg_session, channel="wecom", mode="active", card=card(), delivered=True
        )
        assert count_active_pushes_since(user_id, pg_session, since=NOW - timedelta(days=1)) == 1

    def test_shadow_push_does_not_count_against_the_gate(self, pg_session, user_id):
        """影子模式是"本来会推但没推",不占频率额度。"""
        record_push(
            user_id, pg_session, channel="wecom", mode="shadow", card=card(), delivered=False
        )
        assert count_active_pushes_since(user_id, pg_session, since=NOW - timedelta(days=1)) == 0

    def test_failed_delivery_does_not_count_either(self, pg_session, user_id):
        record_push(
            user_id,
            pg_session,
            channel="wecom",
            mode="active",
            card=card(),
            delivered=False,
            error="企微超时",
        )
        assert count_active_pushes_since(user_id, pg_session, since=NOW - timedelta(days=1)) == 0

    def test_bogus_mode_is_rejected(self, pg_session, user_id):
        with pytest.raises(ValueError):
            record_push(
                user_id, pg_session, channel="wecom", mode="试试", card=card(), delivered=True
            )
