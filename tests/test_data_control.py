"""自己关掉采集、自己删掉数据(P4 第 3 片)。需要真实 PostgreSQL。

**这一组是 R10 改判那四个前提里的第 2 件**,而那节写着"少一件就不该开放"。
所以它测的不是一个功能好不好用,是一个**开放的前置条件成不成立**。

两组:

- **关得掉**:三层各挡一种失效方式,只做一层是不够的
- **删得干净**:是真删不是标记,而且派生的东西一起走 ——
  只删原文会留下一堆"看起来仍然有出处"的记忆,点开才发现出处没了
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from lifein.repos import collector, credentials, data_control, facts, transactions
from lifein.repos.transactions import Direction, TxnKind
from tests.conftest import NOW, api_settings, bearer

pytestmark = pytest.mark.integration

DEVICE = "pixel-7a"
WHEN = NOW - timedelta(days=1)


def a_collector_credential(session, user_id, *, device_id: str = DEVICE):
    import base64
    import os

    credentials.put_credential(
        user_id,
        session,
        kind="collector",
        scope="ingest",
        payload={"secret": base64.b64encode(os.urandom(32)).decode()},
        settings=api_settings(),
        device_id=device_id,
    )


def a_whitelist_rule(session, user_id, *, pattern: str = "com.tencent.mm"):
    return collector.add_whitelist(
        user_id,
        session,
        match_type=collector.MATCH_PACKAGE,
        pattern=pattern,
        purpose=collector.PURPOSE_MESSAGE,
        phase="P1",
    )


def a_collected_event(
    session, user_id, *, external_id: str = "n-1", when=WHEN, source="notification"
):
    return session.execute(
        text(
            "INSERT INTO raw_events (user_id, source, external_id, occurred_at, trust, raw)"
            " VALUES (:u, :s, :e, :t, 'external', '{}'::jsonb) RETURNING id"
        ),
        {"u": user_id, "s": source, "e": external_id, "t": when},
    ).scalar_one()


class TestTurningItOff:
    def test_a_fresh_setup_is_collecting(self, pg_session, user_id):
        a_collector_credential(pg_session, user_id)
        a_whitelist_rule(pg_session, user_id)

        assert data_control.state(user_id, pg_session).enabled is True

    def test_stopping_revokes_the_keys_and_disables_the_rules(self, pg_session, user_id):
        """**只做手机端那一层是不够的。** App 有 bug、被降级、被别人装了旧版本,
        上报都可能继续 —— 所以服务端这边同时吊销密钥并停白名单。"""
        a_collector_credential(pg_session, user_id)
        a_whitelist_rule(pg_session, user_id)

        after = data_control.stop_collecting(user_id, pg_session)

        assert after.enabled is False
        assert after.active_devices == 0
        assert after.enabled_rules == 0

    def test_it_stops_every_device_not_just_one(self, pg_session, user_id):
        """**用户想的是"别再采了",不是"停掉某一台"。** 要求他先列出自己
        有几台设备再一台台停,等于把这个开关做成了一道作业。"""
        a_collector_credential(pg_session, user_id, device_id="phone-1")
        a_collector_credential(pg_session, user_id, device_id="phone-2")
        a_whitelist_rule(pg_session, user_id)

        assert data_control.stop_collecting(user_id, pg_session).active_devices == 0

    def test_the_query_credential_survives(self, pg_session, user_id):
        """**关采集不等于关 App。** 关掉之后他还要能看自己的待办、
        还要能删数据 —— 而删数据要用查询那把密钥。"""
        import base64
        import os

        credentials.put_credential(
            user_id,
            pg_session,
            kind="app_device",
            scope="query",
            payload={"secret": base64.b64encode(os.urandom(32)).decode()},
            settings=api_settings(),
            device_id=DEVICE,
        )
        a_collector_credential(pg_session, user_id)
        data_control.stop_collecting(user_id, pg_session)

        alive = [
            row for row in credentials.list_device_credentials(user_id, pg_session)
            if row.kind == "app_device" and row.revoked_at is None
        ]
        assert len(alive) == 1

    def test_rules_are_disabled_not_deleted(self, pg_session, user_id):
        """**成本一高,人就不敢关了** —— 而 R10 改判要的正是"他敢关"。
        删掉的规则要一条条重新加,那是关掉这个动作最大的成本。"""
        a_whitelist_rule(pg_session, user_id)
        data_control.stop_collecting(user_id, pg_session)

        rules = collector.list_whitelist(user_id, pg_session)
        assert len(rules) == 1
        assert rules[0].enabled is False

    def test_stopping_does_not_delete_data(self, pg_session, user_id):
        """**"我想先停下来想想"不该变成"要么继续采要么全删"。**"""
        a_collected_event(pg_session, user_id)
        data_control.stop_collecting(user_id, pg_session)

        assert _events(pg_session, user_id) == 1


class TestDeleting:
    def test_it_really_deletes(self, pg_session, user_id):
        """**留一份"以防万一"就等于没删** —— 而那正是隐私说明里
        最不该出现的一句话。"""
        a_collected_event(pg_session, user_id)

        removed = data_control.delete_collected(user_id, pg_session)

        assert removed.raw_events == 1
        assert _events(pg_session, user_id) == 0

    def test_derived_transactions_go_too(self, pg_session, user_id):
        event_id = a_collected_event(pg_session, user_id)
        transactions.record(
            user_id,
            pg_session,
            occurred_at=WHEN,
            amount=__import__("decimal").Decimal("38.50"),
            direction=Direction.DEBIT,
            kind=TxnKind.EXPENSE,
            channel="bank_sms",
            source_event_id=event_id,
            confidence=1.0,
        )

        removed = data_control.delete_collected(user_id, pg_session)
        assert removed.transactions == 1

    def test_memories_whose_only_source_is_gone_go_too(self, pg_session, user_id):
        """**只删原文会留下一堆"看起来仍然有出处"的记忆条目**,
        点开才发现出处没了 —— 那比留着原文更糟:它让"有出处"变得不可信。"""
        event_id = a_collected_event(pg_session, user_id)
        _a_fact(pg_session, user_id, statement="不吃香菜", provenance=[event_id])

        removed = data_control.delete_collected(user_id, pg_session)
        assert removed.facts == 1

    def test_memories_with_another_source_stay(self, pg_session, user_id):
        """**那条事实不只来自被删掉的这些。** 删了就是删过头了 ——
        而删过头没有任何地方看得出来。"""
        collected = a_collected_event(pg_session, user_id, external_id="n-1")
        from_email = a_collected_event(
            pg_session, user_id, external_id="mail-1", source="email"
        )
        _a_fact(
            pg_session, user_id, statement="周五要交报告",
            provenance=[collected, from_email],
        )

        removed = data_control.delete_collected(user_id, pg_session)
        assert removed.facts == 0

    def test_email_and_calendar_are_not_touched(self, pg_session, user_id):
        """**这个开关关的是手机上那一路。** 邮件和日历是你自己的账号,
        而 R10 说的是采集器(见 `COLLECT_SOURCES`)。"""
        a_collected_event(pg_session, user_id, external_id="n-1")
        a_collected_event(pg_session, user_id, external_id="mail-1", source="email")

        data_control.delete_collected(user_id, pg_session)
        assert _events(pg_session, user_id) == 1

    def test_deleting_only_recent_days(self, pg_session, user_id):
        """**只能全删的删除按钮很多人不敢点。** "把上周那几天删掉"是一个
        真实的诉求。"""
        a_collected_event(pg_session, user_id, external_id="old", when=NOW - timedelta(days=30))
        a_collected_event(pg_session, user_id, external_id="new", when=NOW - timedelta(hours=2))

        removed = data_control.delete_collected(
            user_id, pg_session, since=NOW - timedelta(days=1)
        )

        assert removed.raw_events == 1
        assert _events(pg_session, user_id) == 1

    def test_nothing_to_delete_is_not_an_error(self, pg_session, user_id):
        assert data_control.delete_collected(user_id, pg_session).total() == 0

    def test_it_does_not_reach_across_users(self, pg_session, user_id):
        """铁律 1。**删除是这条链路上最不能串的动作** ——
        串了的话别人的数据没了,而没有任何地方留下痕迹。"""
        a_collected_event(pg_session, user_id)
        other = "99999999-9999-9999-9999-999999999999"

        assert data_control.delete_collected(other, pg_session).total() == 0
        assert _events(pg_session, user_id) == 1


class TestFromTheApp:
    """**"App 里要有这个开关,不是'找你帮忙'"** —— R10 改判的原话。"""

    def test_the_state_is_readable(self, client, pg_session, user_id, token):
        a_collector_credential(pg_session, user_id)
        a_whitelist_rule(pg_session, user_id)

        body = client.get("/app/collector/collection", headers=bearer(token)).json()
        assert body["enabled"] is True

    def test_stopping_from_the_app_works(self, client, pg_session, user_id, token):
        a_collector_credential(pg_session, user_id)
        a_whitelist_rule(pg_session, user_id)

        body = client.post("/app/collector/stop", headers=bearer(token)).json()

        assert body["enabled"] is False
        assert "还在" in body["note"]  # 说清楚数据没被一起删掉

    def test_deleting_from_the_app_works(self, client, pg_session, user_id, token):
        a_collected_event(pg_session, user_id)

        body = client.request("DELETE", "/app/data/collected", headers=bearer(token)).json()

        assert body["raw_events"] == 1
        assert _events(pg_session, user_id) == 0

    def test_a_naive_since_is_refused(self, client, pg_session, user_id, token):
        response = client.request(
            "DELETE",
            "/app/data/collected?since=2026-09-01T00:00:00",
            headers=bearer(token),
        )
        assert response.status_code == 422

    def test_it_needs_a_token(self, client, pg_session, user_id):
        """**删除必须要凭据。** 这是这个系统里破坏力最大的一个接口。"""
        assert client.post("/app/collector/stop").status_code == 401
        assert client.request("DELETE", "/app/data/collected").status_code == 401


def _a_fact(session, user_id, *, statement: str, provenance: list[int]):
    from lifein.models.normalized import Trust

    return facts.add_fact(
        user_id,
        session,
        statement=statement,
        provenance=provenance,
        confidence=0.6,
        trust=Trust.EXTERNAL,
        created_by_agent="memory",
        valid_from=WHEN,
    )


def _events(session, user_id) -> int:
    return session.execute(
        text("SELECT count(*) FROM raw_events WHERE user_id = :u"), {"u": user_id}
    ).scalar_one()
