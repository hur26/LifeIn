"""手机系统日历的归一化与上报(06 §6.14)。

**日历数据源换人了**:企微日程整个退出(ADR-026),换成从手机的系统日历读。
换过来的不是凑合的替代品 —— 系统日历里有企微同步过来的、飞书的、订阅的、
手动建的**全部**日程,而企微那条只看得见企微自己那一个。

这一组盯两件事:

- **归一化那一半**(不依赖 Android 接口形态)—— 接口变了要改的是 Kotlin
- **解不开的那一条仍然入库**,因为修好之后要能按同一个键重跑(06 §1.4)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from lifein.models.normalized import EventKind, PartyRole, Trust
from lifein.sources.device_calendar import CANCELLED_PREFIX, MAX_BODY, normalize
from tests.conftest import DEVICE, NOW, signed

RECEIVED = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def an_event(**overrides) -> dict:
    payload = {
        "event_id": "gmail.com/8f3a2b",
        "calendar": "白杨的日历",
        "title": "周三下午三点评审",
        "description": "腾讯会议 123-456",
        "location": "三楼会议室",
        "starts_at": "2026-09-11T15:00:00+08:00",
        "ends_at": "2026-09-11T16:00:00+08:00",
        "organizer": "boss@example.com",
        "attendees": ["me@example.com", "boss@example.com"],
        "self_organized": False,
        "cancelled": False,
    }
    payload.update(overrides)
    return payload


class TestNormalizing:
    def test_a_normal_event(self):
        got = normalize(an_event(), received_at=RECEIVED)

        assert got.normalized is not None
        assert got.normalized.kind is EventKind.CALENDAR_EVENT
        assert got.normalized.title == "周三下午三点评审"
        assert got.external_id == "gmail.com/8f3a2b"
        assert got.occurred_at == datetime.fromisoformat("2026-09-11T15:00:00+08:00")
        assert got.normalized.location == "三楼会议室"

    def test_someone_elses_invite_is_external(self):
        """**别人写的标题和描述,和一封邮件没有区别**(R3)。

        它会被包进隔离标记送进模型,而"这是别人写的"正是那道标记的依据。
        """
        assert normalize(an_event(), received_at=RECEIVED).trust is Trust.EXTERNAL

    def test_your_own_event_is_user_input(self):
        got = normalize(an_event(self_organized=True), received_at=RECEIVED)
        assert got.trust is Trust.USER_INPUT

    def test_the_organizer_and_attendees_become_parties(self):
        """标识类型是邮箱。**这一点比企微那版好** —— 邮箱能和邮件那条链路上的
        人对上,而企微 userid 只在企微里有意义。"""
        parties = normalize(an_event(), received_at=RECEIVED).normalized.parties

        roles = {p.role for p in parties}
        assert PartyRole.ORGANIZER in roles
        assert PartyRole.ATTENDEE in roles
        # 组织者不会在参与者里再出现一次 —— 系统日历两处都会列他
        assert [p.display_name for p in parties].count("boss@example.com") == 1

    def test_a_cancelled_event_is_kept_and_marked(self):
        """**一个本来要去的会被取消,恰恰是当天最该知道的事之一** ——
        不能因为"它不会发生"就丢掉。"""
        got = normalize(an_event(cancelled=True), received_at=RECEIVED)

        assert got.normalized is not None
        assert got.normalized.title.startswith(CANCELLED_PREFIX)


class TestWhatCannotBeUsed:
    """**解不开的照样入库。** 一条坏日程不该让同一批的另外九十九条一起退回去,
    而"它当时长什么样"要留在 `raw` 里 —— 否则修好之后没法重跑。"""

    def test_no_id_means_no_dedup_key(self):
        got = normalize(an_event(event_id=""), received_at=RECEIVED)

        assert got.normalized is None
        assert "去重键" in got.normalize_error
        assert got.raw["title"] == "周三下午三点评审"  # 原样留着

    def test_two_broken_ones_do_not_collide(self):
        """**没有 id 的那些不能撞成同一行。**

        用固定占位的话只留得下第一条,而"当时报上来了几条坏的"是查这类问题
        的第一个数字。
        """
        first = normalize(an_event(event_id=None), received_at=RECEIVED)
        second = normalize(
            an_event(event_id=None), received_at=RECEIVED.replace(second=1)
        )
        assert first.external_id != second.external_id

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "2026-09-11",  # 只有日期
            "下周三",
            "2026-09-11T15:00:00",  # **不带时区**
        ],
    )
    def test_a_time_without_a_zone_is_refused(self, bad):
        """不带时区的话服务器和手机在不同时区时,一场晚上八点的会会落到别的
        一天 —— 而月度报表和每日摘要都按天切,那种错在界面上完全看不出来。"""
        got = normalize(an_event(starts_at=bad), received_at=RECEIVED)

        assert got.normalized is None
        assert "starts_at" in got.normalize_error
        # 有 id 的那些仍然按 id 落库 —— 修好之后按同一个键重跑
        assert got.external_id == "gmail.com/8f3a2b"

    def test_a_giant_description_is_truncated_not_dropped(self):
        """日程描述里常常整段贴着会议纪要。**留全了会把摘要的素材预算吃光**,
        而判断"这是什么会"用不到第两千个字。`raw` 里那份是全的。"""
        long = "议程" * 3000
        got = normalize(an_event(description=long), received_at=RECEIVED)

        assert len(got.normalized.body) == MAX_BODY
        assert got.raw["description"] == long


class TestTheEndpoint:
    """`POST /ingest/calendar`。需要真实 PostgreSQL。"""

    pytestmark = pytest.mark.integration

    def post(self, client, pg_session, user_id, secrets, events):
        body = {"device_id": DEVICE, "events": events}
        return signed(
            client,
            "/ingest/calendar",
            body,
            user_id=user_id,
            secret=secrets["ingest"],
        )

    def test_it_lands_in_raw_events(self, client, pg_session, user_id, secrets):
        response = self.post(client, pg_session, user_id, secrets, [an_event()])

        assert response.status_code == 200
        assert response.json()["accepted"] == 1
        row = pg_session.execute(
            text("SELECT source, external_id FROM raw_events WHERE user_id = :u"),
            {"u": user_id},
        ).one()
        assert (row.source, row.external_id) == ("calendar", "gmail.com/8f3a2b")

    def test_the_same_event_twice_is_one_row(self, client, pg_session, user_id, secrets):
        """**日历会被反复扫。** 每次上报都是全窗口的那几十条,
        而其中绝大多数上一轮就报过了 —— 去重键是这条链路的日常路径,不是异常。
        """
        self.post(client, pg_session, user_id, secrets, [an_event()])
        again = self.post(client, pg_session, user_id, secrets, [an_event()])

        assert again.json() == {"accepted": 0, "duplicates": 1, "unusable": 0}

    def test_a_broken_one_does_not_take_the_batch_down(
        self, client, pg_session, user_id, secrets
    ):
        response = self.post(
            client,
            pg_session,
            user_id,
            secrets,
            [an_event(), an_event(event_id="x", starts_at="下周三")],
        )

        body = response.json()
        assert body["accepted"] == 2, "解不开的也入库 —— 修好之后要能重跑"
        assert body["unusable"] == 1

    def test_the_query_credential_cannot_post_it(self, client, pg_session, user_id, secrets):
        """铁律 12:采集与查询分开签发,而这是采集那一路。

        反过来那条(采集凭据读不到账本)在 `test_api_query.py` 里 ——
        两条一起才是 R11 那句"最重要的一条"。
        """
        response = signed(
            client,
            "/ingest/calendar",
            {"device_id": DEVICE, "events": [an_event()]},
            user_id=user_id,
            secret=secrets["query"],
        )
        assert response.status_code == 401
        assert response.content == b""

    def test_another_devices_id_is_refused(self, client, pg_session, user_id, secrets):
        """body 里的 `device_id` 是对方说了算的,而签名认出来的那个不是。
        两个对不上就拒 —— 否则一台设备能冒充另一台去污染它的去重键。

        **422 而不是静默采信签名那个**:不一致多半是设备端串了状态
        (比如恢复备份带过来了别人的 device_id),而静默纠正会让那个 bug
        永远查不出来。
        """
        response = signed(
            client,
            "/ingest/calendar",
            {"device_id": "别人的手机", "events": [an_event()]},
            user_id=user_id,
            secret=secrets["ingest"],
        )
        assert response.status_code == 422


def test_now_is_a_dependency_not_a_call():
    """`received_at` 由调用方给。

    它只在"这条解不开"时被用作 `occurred_at`,而那时它决定这条坏事件落在
    哪一天 —— 测试里定不住的话,那两条"坏日程不撞车"的用例就是碰运气。
    """
    assert NOW.tzinfo is not None
