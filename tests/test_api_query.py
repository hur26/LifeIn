"""查询端的集成测试(06 §6.3–§6.10)。

需要真实 PostgreSQL:待确认那条"写入与状态更新同一个事务"只有在真库上
才成立,而它正是这一组最要紧的用例。

**第一组用例是 P1 验收标准里那条"实测"**:采集凭据读不到任何东西。
写在最前面是因为它一旦失效,后面所有功能都无所谓了。
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
from lifein.bootstrap import Services, register_tools
from lifein.config import Settings
from lifein.repos import collector, credentials, pending, todos
from tests.test_config import BASE

pytestmark = pytest.mark.integration

DEVICE = "pixel-7a"
NOW = datetime(2026, 9, 8, 2, 0, 0, tzinfo=UTC)  # 北京时间 10:00


def settings() -> Settings:
    return Settings(_env_file=None, **BASE)


@pytest.fixture(autouse=True)
def tools_registered():
    """确认待确认那条要过网关,网关要查得到 planner 的白名单。"""
    register_tools()


@pytest.fixture
def secrets(pg_session, user_id) -> dict[str, str]:
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
def client(pg_session) -> TestClient:
    app = create_app(Services(settings=None, llm=None, channel=None, alerter=None))
    app.dependency_overrides[get_session] = lambda: pg_session
    app.dependency_overrides[get_app_settings] = settings
    app.dependency_overrides[now_utc] = lambda: NOW
    return TestClient(app)


def signed(client, path, body, *, user_id, secret, when=NOW):
    payload = json.dumps(body).encode()
    timestamp = str(int(when.timestamp()))
    message = auth.signing_string(method="POST", path=path, timestamp=timestamp, body=payload)
    return client.post(
        path,
        content=payload,
        headers={
            "Content-Type": "application/json",
            auth.HEADER_USER: user_id,
            auth.HEADER_DEVICE: DEVICE,
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


class TestTheIngestCredentialCannotRead:
    """R11 那句"最重要的一条",以及 P1 验收标准里的实测项。"""

    def test_ingest_secret_cannot_mint_a_query_token(self, client, user_id, secrets):
        response = signed(
            client, "/app/token", {"device_id": DEVICE}, user_id=user_id, secret=secrets["ingest"]
        )
        assert response.status_code == 401
        assert response.text == ""

    def test_a_token_forged_with_the_ingest_secret_is_rejected(self, client, user_id, secrets):
        forged = auth.mint_token(
            secret=secrets["ingest"],
            user_id=user_id,
            device_id=DEVICE,
            expires_at=NOW + timedelta(hours=1),
        )
        assert client.get("/app/todos", headers=bearer(forged)).status_code == 401

    def test_every_query_route_needs_a_token(self, client):
        for path in ("/app/todos", "/app/pending", "/app/calendar/queue",
                     "/app/collector/status", "/app/memory/facts", "/app/memory/entities"):
            assert client.get(path).status_code == 401


class TestToken:
    def test_expired_token_is_rejected(self, client, user_id, secrets):
        stale = auth.mint_token(
            secret=secrets["query"],
            user_id=user_id,
            device_id=DEVICE,
            expires_at=NOW - timedelta(seconds=1),
        )
        assert client.get("/app/todos", headers=bearer(stale)).status_code == 401

    def test_revoking_the_device_kills_the_token_immediately(
        self, client, pg_session, user_id, token
    ):
        """手机丢了 —— 已经发出去的 token 下一次请求就失效,不用等它过期。"""
        assert client.get("/app/todos", headers=bearer(token)).status_code == 200
        credentials.revoke_device(user_id, pg_session, device_id=DEVICE)
        assert client.get("/app/todos", headers=bearer(token)).status_code == 401


class TestTodos:
    def test_default_window_is_the_end_of_today_in_the_users_timezone(
        self, client, pg_session, user_id, token
    ):
        """小组件读的就是这个默认值。时区按用户的,不按手机的。"""
        todos.create_todo(
            user_id,
            pg_session,
            kind=todos.TodoKind.SCHEDULE,
            title="今天下午的会",
            source=todos.TodoSource.USER,
            starts_at=NOW + timedelta(hours=4),
        )
        todos.create_todo(
            user_id,
            pg_session,
            kind=todos.TodoKind.SCHEDULE,
            title="下周的会",
            source=todos.TodoSource.USER,
            starts_at=NOW + timedelta(days=7),
        )
        todos.create_todo(
            user_id,
            pg_session,
            kind=todos.TodoKind.TODO,
            title="没定时间的事",
            source=todos.TodoSource.USER,
        )

        body = client.get("/app/todos", headers=bearer(token)).json()
        titles = [item["title"] for item in body["todos"]]

        # 没有时间的排在有时间的后面:小组件上方寸之地,几点要到场的更重要
        assert titles == ["今天下午的会", "没定时间的事"]

    def test_user_created_todo_has_no_provenance_and_no_agent(
        self, client, pg_session, user_id, token
    ):
        """用户自己加的不需要出处 —— 他就是出处(铁律 5 的另一半)。"""
        created = client.post(
            "/app/todos", headers=bearer(token), json={"title": "买牙膏"}
        ).json()

        assert created["source"] == "user"
        assert created["kind"] == "todo"
        assert created["created_by_agent"] is None
        # 用户点的写不过网关,所以审计表里不该多出一条(06 §6.6)
        assert _count(pg_session, "tool_calls", user_id) == 0

    def test_completing_and_cancelling(self, client, pg_session, user_id, token):
        created = client.post(
            "/app/todos", headers=bearer(token), json={"title": "交房租"}
        ).json()

        done = client.post(
            f"/app/todos/{created['id']}/status", headers=bearer(token), json={"status": "done"}
        )
        assert done.json()["status"] == "done"

        # 撤销不删行:删了就没人知道设备上还有一条要清理
        client.post(
            f"/app/todos/{created['id']}/status",
            headers=bearer(token),
            json={"status": "cancelled"},
        )
        assert todos.get_todo(user_id, pg_session, todo_id=created["id"]) is not None

    def test_unknown_todo_is_a_404(self, client, token):
        missing = "00000000-0000-0000-0000-000000000000"
        response = client.post(
            f"/app/todos/{missing}/status", headers=bearer(token), json={"status": "done"}
        )
        assert response.status_code == 404


class TestPending:
    def queue_one(self, pg_session, user_id, **overrides):
        payload = {
            "title": "周三下午三点开会",
            "starts_at": (NOW + timedelta(days=1)).isoformat(),
            "provenance": [self.event_id(pg_session, user_id)],
            "created_by_agent": "planner",
            **overrides,
        }
        return pending.enqueue(
            user_id,
            pg_session,
            agent="planner",
            kind=pending.PendingKind.CALENDAR_EVENT,
            target_table="todos",
            payload=payload,
            reason=pending.PendingReason.LOW_CONFIDENCE,
            confidence=0.5,
            now=NOW,
        )

    def event_id(self, pg_session, user_id) -> int:
        return pg_session.execute(
            text(
                "INSERT INTO raw_events (user_id, source, external_id, occurred_at, trust, raw)"
                " VALUES (:u, 'email', :e, :t, 'external', '{}'::jsonb) RETURNING id"
            ),
            {"u": user_id, "e": f"m-{os.urandom(4).hex()}", "t": NOW},
        ).scalar_one()

    def test_listing_gives_the_payload_as_is(self, client, pg_session, user_id, token):
        self.queue_one(pg_session, user_id)
        body = client.get("/app/pending", headers=bearer(token)).json()

        (item,) = body["pending"]
        assert item["target_table"] == "todos"
        assert item["payload"]["title"] == "周三下午三点开会"
        assert item["reason"] == "low_confidence"

    def test_confirming_writes_the_todo_and_leaves_an_audit_trail(
        self, client, pg_session, user_id, token
    ):
        queued = self.queue_one(pg_session, user_id)

        response = client.post(
            f"/app/pending/{queued.id}/resolve", headers=bearer(token), json={"action": "confirm"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "confirmed"

        (row,) = pg_session.execute(
            text("SELECT title, source, created_by_agent FROM todos WHERE user_id = :u"),
            {"u": user_id},
        ).all()
        assert (row.title, row.source, row.created_by_agent) == (
            "周三下午三点开会",
            "agent",
            "planner",
        )

        # agent 建的必须过网关 —— 那条带 rollback_info 的记录是"怎么撤"的唯一答案
        audit = pg_session.execute(
            text(
                "SELECT tool_name, level, rollback_info FROM tool_calls"
                " WHERE user_id = :u AND tool_name = 'todo.create'"
            ),
            {"u": user_id},
        ).one()
        assert audit.level == "L2"
        assert audit.rollback_info["cancel_todo_id"]

    def test_confirming_twice_is_a_409_not_a_second_write(
        self, client, pg_session, user_id, token
    ):
        """两个入口同时点确认是正常的用户行为,不是错误 —— 但只能写一次。"""
        queued = self.queue_one(pg_session, user_id)
        first = client.post(
            f"/app/pending/{queued.id}/resolve", headers=bearer(token), json={"action": "confirm"}
        )
        second = client.post(
            f"/app/pending/{queued.id}/resolve", headers=bearer(token), json={"action": "confirm"}
        )

        assert (first.status_code, second.status_code) == (200, 409)
        assert _count(pg_session, "todos", user_id) == 1

    def test_editing_cannot_rewrite_the_provenance(self, client, pg_session, user_id, token):
        """出处不由客户端说了算(铁律 5)。"""
        queued = self.queue_one(pg_session, user_id)
        original = list(queued.payload["provenance"])

        client.post(
            f"/app/pending/{queued.id}/resolve",
            headers=bearer(token),
            json={
                "action": "confirm",
                "payload": {
                    "title": "周三下午四点开会",
                    "provenance": [],
                    "created_by_agent": None,
                },
            },
        )

        (row,) = pg_session.execute(
            text("SELECT title, provenance, created_by_agent FROM todos WHERE user_id = :u"),
            {"u": user_id},
        ).all()
        assert row.title == "周三下午四点开会"  # 改得了标题
        assert list(row.provenance) == original  # 改不了出处
        assert row.created_by_agent == "planner"

    def test_rejecting_keeps_the_record(self, client, pg_session, user_id, token):
        """拒绝的记录永不删除:它是那个 agent 评测集的负样本。"""
        queued = self.queue_one(pg_session, user_id)
        response = client.post(
            f"/app/pending/{queued.id}/resolve", headers=bearer(token), json={"action": "reject"}
        )

        assert response.json()["status"] == "rejected"
        assert _count(pg_session, "todos", user_id) == 0
        after = pending.get(user_id, pg_session, pending_id=queued.id)
        assert after.status is pending.PendingStatus.REJECTED

    def test_unknown_target_table_is_refused(self, client, pg_session, user_id, token):
        """P2 的账目进来时,这个版本的服务端不该硬着头皮写。"""
        queued = pending.enqueue(
            user_id,
            pg_session,
            agent="bookkeeper",
            kind=pending.PendingKind.TRANSACTION,
            target_table="transactions",
            payload={"amount": "12.00"},
            reason=pending.PendingReason.LOW_CONFIDENCE,
            now=NOW,
        )
        response = client.post(
            f"/app/pending/{queued.id}/resolve", headers=bearer(token), json={"action": "confirm"}
        )
        assert response.status_code == 422
        # 没被认领:状态还是 pending,将来版本更新了还能处理
        assert pending.get(user_id, pg_session, pending_id=queued.id).status is (
            pending.PendingStatus.PENDING
        )


class TestCalendarSync:
    def schedule(self, pg_session, user_id) -> todos.Todo:
        return todos.create_todo(
            user_id,
            pg_session,
            kind=todos.TodoKind.SCHEDULE,
            title="体检",
            source=todos.TodoSource.USER,
            starts_at=NOW + timedelta(days=2),
        )

    def test_queue_lists_what_the_device_has_not_written_yet(
        self, client, pg_session, user_id, token
    ):
        item = self.schedule(pg_session, user_id)
        body = client.get("/app/calendar/queue", headers=bearer(token)).json()

        assert [entry["todo_id"] for entry in body["to_create"]] == [item.id]
        assert body["to_delete"] == []

    def test_reporting_created_records_the_event_id(self, client, pg_session, user_id, token):
        """那个 id 就是 L2 的回滚信息(ADR-020)。"""
        item = self.schedule(pg_session, user_id)
        response = client.post(
            "/app/calendar/report",
            headers=bearer(token),
            json={"todo_id": item.id, "action": "created", "device_ref": "evt-991"},
        )

        assert response.json() == {"status": "synced"}
        after = todos.get_todo(user_id, pg_session, todo_id=item.id)
        assert after.device_ref == "evt-991"
        assert after.synced_at is not None
        # 落地之后就不该再出现在待写队列里
        body = client.get("/app/calendar/queue", headers=bearer(token)).json()
        assert body["to_create"] == []

    def test_created_without_an_event_id_is_refused(self, client, pg_session, user_id, token):
        item = self.schedule(pg_session, user_id)
        response = client.post(
            "/app/calendar/report",
            headers=bearer(token),
            json={"todo_id": item.id, "action": "created"},
        )
        assert response.status_code == 422

    def test_cancelled_schedule_becomes_a_delete_task_then_clears(
        self, client, pg_session, user_id, token
    ):
        """撤销在服务端只完成一半,另一半在设备上。"""
        item = self.schedule(pg_session, user_id)
        todos.mark_synced(user_id, pg_session, todo_id=item.id, device_ref="evt-991")
        client.post(
            f"/app/todos/{item.id}/status", headers=bearer(token), json={"status": "cancelled"}
        )

        body = client.get("/app/calendar/queue", headers=bearer(token)).json()
        assert body["to_delete"] == [{"todo_id": item.id, "device_ref": "evt-991"}]

        client.post(
            "/app/calendar/report",
            headers=bearer(token),
            json={"todo_id": item.id, "action": "deleted"},
        )
        after = client.get("/app/calendar/queue", headers=bearer(token)).json()
        assert after["to_delete"] == []  # 不会被反复要求删除

    def test_done_schedules_are_not_deleted_from_the_calendar(
        self, client, pg_session, user_id, token
    ):
        """一场已经开完的会该留在日历里 —— 删掉等于篡改历史。"""
        item = self.schedule(pg_session, user_id)
        todos.mark_synced(user_id, pg_session, todo_id=item.id, device_ref="evt-992")
        client.post(
            f"/app/todos/{item.id}/status", headers=bearer(token), json={"status": "done"}
        )

        body = client.get("/app/calendar/queue", headers=bearer(token)).json()
        assert body["to_delete"] == []


class TestCollectorPanel:
    def test_status_shows_heartbeat_and_whitelist(self, client, pg_session, user_id, token):
        collector.record_heartbeat(
            user_id, pg_session, device_id=DEVICE, now=NOW - timedelta(hours=3)
        )
        collector.add_whitelist(
            user_id,
            pg_session,
            match_type=collector.MATCH_PACKAGE,
            pattern="com.tencent.mm",
            purpose=collector.PURPOSE_MESSAGE,
            phase="P1",
        )

        body = client.get("/app/collector/status", headers=bearer(token)).json()

        assert body["devices"][0]["device_id"] == DEVICE
        assert body["devices"][0]["stale"] is True  # 三小时没心跳了
        assert body["whitelist"][0]["pattern"] == "com.tencent.mm"

    def test_whitelist_can_be_added_and_switched_off_but_not_deleted(
        self, client, pg_session, user_id, token
    ):
        rule = client.post(
            "/app/collector/whitelist",
            headers=bearer(token),
            json={
                "match_type": "package_name",
                "pattern": "com.tencent.mm",
                "purpose": "message",
                "phase": "P1",
            },
        ).json()

        client.post(
            f"/app/collector/whitelist/{rule['id']}/enabled",
            headers=bearer(token),
            json={"enabled": False},
        )

        # 停用之后不再放行,但那一行还在 —— 能回答"曾经放行过谁"
        assert collector.list_whitelist(user_id, pg_session, enabled_only=True) == []
        assert len(collector.list_whitelist(user_id, pg_session)) == 1


def _count(session, table: str, user_id: str) -> int:
    return session.execute(
        text(f"SELECT count(*) FROM {table} WHERE user_id = :u"), {"u": user_id}
    ).scalar_one()


class TestMemory:
    """记忆浏览(06 §6.10)。

    这一组盯的是 P1 的退出条件:"记忆里开始出现你不认可又说不清来源的条目"。
    所以每个用例都在问同一件事的两半 —— **改得动吗**、**说得出来源吗**。
    """

    def event(self, pg_session, user_id, title="周五聚餐") -> int:
        return pg_session.execute(
            text(
                "INSERT INTO raw_events (user_id, source, external_id, occurred_at, trust, raw,"
                " normalized) VALUES (:u, 'email', :e, :t, 'external', '{}'::jsonb,"
                " CAST(:n AS JSONB)) RETURNING id"
            ),
            {
                "u": user_id,
                "e": f"m-{os.urandom(4).hex()}",
                "t": NOW,
                "n": json.dumps({"title": title}, ensure_ascii=False),
            },
        ).scalar_one()

    def fact(self, pg_session, user_id, statement="不吃香菜", title="周五聚餐"):
        from lifein.models.normalized import Trust
        from lifein.repos import facts

        return facts.add_fact(
            user_id,
            pg_session,
            statement=statement,
            provenance=[self.event(pg_session, user_id, title=title)],
            confidence=0.9,
            trust=Trust.EXTERNAL,
            created_by_agent="memory",
            valid_from=NOW,
        ).fact

    def test_facts_come_with_their_sources(self, client, pg_session, user_id, token):
        """出处和事实并排给出 —— 那是"说不清来源"这条退出条件唯一的日常验证形式。"""
        created = self.fact(pg_session, user_id)

        body = client.get("/app/memory/facts", headers=bearer(token)).json()

        (item,) = body["facts"]
        assert item["statement"] == "不吃香菜"
        # external 推出来的封顶 0.6(06 §1.2)
        assert item["confidence"] == 0.6
        assert item["provenance"] == created.provenance

        source = body["sources"][str(created.provenance[0])]
        assert (source["source"], source["title"]) == ("email", "周五聚餐")

    def test_search_filters(self, client, pg_session, user_id, token):
        self.fact(pg_session, user_id, statement="不吃香菜")
        self.fact(pg_session, user_id, statement="每周三晚上健身")

        body = client.get("/app/memory/facts?q=健身", headers=bearer(token)).json()
        assert [f["statement"] for f in body["facts"]] == ["每周三晚上健身"]

    def test_confirm_is_the_only_way_past_the_cap(self, client, pg_session, user_id, token):
        created = self.fact(pg_session, user_id)

        after = client.post(
            f"/app/memory/facts/{created.id}/confirm", headers=bearer(token)
        ).json()

        assert after["confirmed_by_user"] is True
        assert after["confidence"] == 1.0

    def test_negate_hides_it_but_keeps_the_row(self, client, pg_session, user_id, token):
        """否定只标记不删 —— 删了明天会被重新推断出来(R7)。"""
        from lifein.repos import facts

        created = self.fact(pg_session, user_id)
        client.post(f"/app/memory/facts/{created.id}/negate", headers=bearer(token))

        body = client.get("/app/memory/facts", headers=bearer(token)).json()
        assert body["facts"] == []
        assert facts.get_fact(user_id, pg_session, fact_id=created.id).negated_by_user is True

    def test_correcting_keeps_the_provenance_and_negates_the_old_one(
        self, client, pg_session, user_id, token
    ):
        """改的是说法,不是出处(铁律 5)。"""
        from lifein.repos import facts

        created = self.fact(pg_session, user_id, statement="不吃香菜")

        corrected = client.post(
            f"/app/memory/facts/{created.id}/correct",
            headers=bearer(token),
            json={"statement": "不吃香菜也不吃芹菜"},
        ).json()

        assert corrected["statement"] == "不吃香菜也不吃芹菜"
        assert corrected["provenance"] == created.provenance  # 出处照抄
        assert corrected["confirmed_by_user"] is True
        assert corrected["confidence"] == 1.0
        # 用户亲手写的那条,记的不是某个 agent
        assert corrected["created_by_agent"] == "user"
        # 旧那条被否定,不是被改掉:系统当初推断出了什么要留得住
        assert facts.get_fact(user_id, pg_session, fact_id=created.id).negated_by_user is True

    def test_correcting_into_something_previously_negated_is_refused(
        self, client, pg_session, user_id, token
    ):
        old = self.fact(pg_session, user_id, statement="讨厌香菜")
        client.post(f"/app/memory/facts/{old.id}/negate", headers=bearer(token))

        another = self.fact(pg_session, user_id, statement="不吃香菜")
        response = client.post(
            f"/app/memory/facts/{another.id}/correct",
            headers=bearer(token),
            json={"statement": "讨厌香菜"},
        )
        # 用户自己否定过这句话,照他的判断办
        assert response.status_code == 409

    def test_unknown_fact_is_a_404(self, client, token):
        missing = "00000000-0000-0000-0000-000000000000"
        assert client.post(
            f"/app/memory/facts/{missing}/negate", headers=bearer(token)
        ).status_code == 404

    def test_entities_are_read_only(self, client, pg_session, user_id, token):
        """别名归并走规则(铁律 9),不该在手机上手工编 —— 所以只有 GET。"""
        pg_session.execute(
            text(
                "INSERT INTO entities (user_id, kind, canonical_name, first_seen_at, last_seen_at)"
                " VALUES (:u, 'person', '张三', :t, :t)"
            ),
            {"u": user_id, "t": NOW},
        )

        body = client.get("/app/memory/entities?q=张", headers=bearer(token)).json()
        assert body["entities"][0]["canonical_name"] == "张三"

        # 没有写实体的路由
        assert client.post("/app/memory/entities", headers=bearer(token), json={}).status_code in (
            404,
            405,
        )
