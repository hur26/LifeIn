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


class TestWhatElseHasToGo:
    """**"删除按钮点了,东西还在"是这一节存在的理由。**

    最初的派生清单只有交易、待确认、记忆三样,而从那些通知派生出来的
    还有三样:日程、向量、别名证据。漏掉它们的表现都一样 ——
    用户以为删掉了,而那些东西还在库里,有的还能被检索命中。
    """

    def a_todo_from(self, session, user_id, *event_ids):
        return session.execute(
            text(
                "INSERT INTO todos (user_id, kind, title, status, source, provenance,"
                " created_by_agent, starts_at)"
                " VALUES (:u, 'schedule', '周三三点开会', 'open', 'agent',"
                " CAST(:p AS BIGINT[]), 'planner', :t) RETURNING id"
            ),
            {"u": user_id, "p": list(event_ids), "t": WHEN},
        ).scalar_one()

    def a_vector(self, session, user_id, *, ref_type: str, ref_id: str):
        session.execute(
            text(
                "INSERT INTO embeddings (user_id, ref_type, ref_id, embedding, model)"
                " VALUES (:u, :rt, :ri, CAST(:v AS VECTOR), 'test-model')"
            ),
            {
                "u": user_id,
                "rt": ref_type,
                "ri": ref_id,
                "v": "[" + ",".join(["0.1"] * 1024) + "]",
            },
        )

    def an_alias(self, session, user_id, *event_ids):
        entity = session.execute(
            text(
                "INSERT INTO entities (user_id, kind, canonical_name,"
                " first_seen_at, last_seen_at)"
                " VALUES (:u, 'person', '老王', :t, :t) RETURNING id"
            ),
            {"u": user_id, "t": WHEN},
        ).scalar_one()
        return session.execute(
            text(
                "INSERT INTO entity_aliases (user_id, entity_id, alias, alias_type,"
                " evidence_event_ids) VALUES (:u, :e, '老王', 'nickname',"
                " CAST(:v AS BIGINT[])) RETURNING id"
            ),
            {"u": user_id, "e": entity, "v": list(event_ids)},
        ).scalar_one()

    def count(self, session, table, user_id) -> int:
        return session.execute(
            text(f"SELECT count(*) FROM {table} WHERE user_id = :u"), {"u": user_id}
        ).scalar_one()

    def test_a_schedule_from_a_deleted_notification_goes_too(self, pg_session, user_id):
        """**日程还在日历里,说"来自某条通知",而那条通知没了。**
        和记忆条目是同一个问题:它看起来仍然有出处。"""
        event = a_collected_event(pg_session, user_id)
        self.a_todo_from(pg_session, user_id, event)

        deleted = data_control.delete_collected(user_id, pg_session)

        assert deleted.todos == 1
        assert self.count(pg_session, "todos", user_id) == 0

    def test_a_schedule_with_another_source_stays(self, pg_session, user_id):
        """出处不止这些事件的留着 —— 删了反而是删掉了邮件那一半。"""
        collected = a_collected_event(pg_session, user_id)
        from_email = a_collected_event(
            pg_session, user_id, external_id="m-1", source="email"
        )
        self.a_todo_from(pg_session, user_id, collected, from_email)

        deleted = data_control.delete_collected(user_id, pg_session)

        assert deleted.todos == 0
        assert self.count(pg_session, "todos", user_id) == 1

    def test_the_vector_of_a_deleted_event_goes_too(self, pg_session, user_id):
        """**"删了但向量还在"不算删掉。**

        向量是原文的有损编码,而且留着的那条还能被语义检索命中 ——
        于是问一句相关的话,那条本该消失的东西又浮上来了。
        """
        event = a_collected_event(pg_session, user_id)
        self.a_vector(pg_session, user_id, ref_type="raw_event", ref_id=str(event))

        deleted = data_control.delete_collected(user_id, pg_session)

        assert deleted.embeddings == 1
        assert self.count(pg_session, "embeddings", user_id) == 0

    def test_the_vector_of_a_deleted_fact_goes_too(self, pg_session, user_id):
        """事实的向量在事实之前删 —— 反过来的话那条子查询已经查不到东西了,
        而向量会安静地留下来。"""
        event = a_collected_event(pg_session, user_id)
        added = _a_fact(pg_session, user_id, statement="他不吃香菜", provenance=[event])
        self.a_vector(pg_session, user_id, ref_type="fact", ref_id=str(added.fact.id))

        deleted = data_control.delete_collected(user_id, pg_session)

        assert (deleted.facts, deleted.embeddings) == (1, 1)
        assert self.count(pg_session, "embeddings", user_id) == 0

    def test_an_unrelated_vector_stays(self, pg_session, user_id):
        a_collected_event(pg_session, user_id)
        self.a_vector(pg_session, user_id, ref_type="raw_event", ref_id="999999")

        data_control.delete_collected(user_id, pg_session)

        assert self.count(pg_session, "embeddings", user_id) == 1

    def test_deleted_events_are_stripped_from_alias_evidence(self, pg_session, user_id):
        """**改不是删。** 一个别名可能有好几条证据,删掉整条等于把别的
        证据也一起丢了。"""
        collected = a_collected_event(pg_session, user_id)
        from_email = a_collected_event(
            pg_session, user_id, external_id="m-1", source="email"
        )
        alias = self.an_alias(pg_session, user_id, collected, from_email)

        deleted = data_control.delete_collected(user_id, pg_session)

        assert deleted.aliases == 0
        evidence = pg_session.execute(
            text("SELECT evidence_event_ids FROM entity_aliases WHERE id = :i"),
            {"i": alias},
        ).scalar_one()
        assert evidence == [from_email]

    def test_an_alias_with_no_evidence_left_is_removed(self, pg_session, user_id):
        """**一条没有任何证据的别名比没有这条别名更糟**:它会继续把"老王"
        解析到某个实体上,而没有任何东西能解释凭什么。"""
        event = a_collected_event(pg_session, user_id)
        self.an_alias(pg_session, user_id, event)

        deleted = data_control.delete_collected(user_id, pg_session)

        assert deleted.aliases == 1
        assert self.count(pg_session, "entity_aliases", user_id) == 0

    def test_the_audit_trail_stays(self, pg_session, user_id):
        """**审计是保护用户的记录,不是关于用户的记录。**

        里面只有字段名和长度,没有内容 —— 而删掉它等于让"它到底把什么发给了
        外部模型"这个问题永远没法回答(R12)。这一条写进了隐私说明,
        所以它必须是真的。
        """
        a_collected_event(pg_session, user_id)
        pg_session.execute(
            text(
                "INSERT INTO tool_calls (user_id, agent, tool_name, level,"
                " args_digest, result_status) VALUES (:u, 'digest', 'llm.chat', 'L1',"
                " CAST('{}' AS JSONB), 'allowed')"
            ),
            {"u": user_id},
        )

        data_control.delete_collected(user_id, pg_session)

        assert self.count(pg_session, "tool_calls", user_id) == 1
