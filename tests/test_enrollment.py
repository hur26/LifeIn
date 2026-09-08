"""一次性换取码配码(P4 第 1、2 片,06 §6.15)。需要真实 PostgreSQL。

**这一片补的是今天就存在的一个洞。** `admin issue-device` 打出来的二维码里是
明文的两把密钥 —— 自己扫没问题,而 03 的 P4 说朋友要用它配码,
那张图会走微信发过去,**等于把密钥发在聊天里**。

所以这一组盯三件事:

1. **一次性**:换过一次立刻作废,第二次一律 401
2. **短命**:过期的换不了
3. **不泄露**:码不对、用过了、过期了长得一模一样 ——
   区分等于告诉对方"这个码存在过",而那是爆破时唯一有用的信息
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from lifein.repos import enrollment
from tests.conftest import NOW, api_settings

pytestmark = pytest.mark.integration

DEVICE = "pixel-generated-a1b2c3"


def a_code(session, user_id, *, purpose: str = "all", ttl=timedelta(minutes=10)):
    return enrollment.issue(
        user_id, session, base_url="https://lifein.example.com", now=NOW,
        purpose=purpose, ttl=ttl,
    )


class TestTheCodeItself:
    def test_the_plaintext_is_never_stored(self, pg_session, user_id):
        """**和存密码一样。** 库被拖走时,里面的东西不该直接可用 ——
        服务端自己也说不出这个码是什么。"""
        code, _ = a_code(pg_session, user_id)

        stored = pg_session.execute(
            text("SELECT code_hash FROM enrollment_codes WHERE user_id = :u"), {"u": user_id}
        ).scalar_one()
        assert code not in stored
        assert len(stored) == 64  # sha256 的十六进制

    def test_it_is_long_enough_that_brute_force_is_not_a_thing(self, pg_session, user_id):
        """**防爆破靠长度,不靠限流。** 限流要存计数状态,那又是一张表,
        换来的是对一个已经穷举不动的东西再加一道。"""
        code, _ = a_code(pg_session, user_id)
        assert len(code) >= 20  # 128 位随机数的 urlsafe 编码

    def test_two_codes_are_never_the_same(self, pg_session, user_id):
        first, _ = a_code(pg_session, user_id)
        second, _ = a_code(pg_session, user_id)
        assert first != second


class TestClaimingOnce:
    def test_a_fresh_code_hands_out_two_secrets(self, pg_session, user_id):
        """铁律 12:采集与查询是两条独立的行、两把独立的密钥。"""
        code, _ = a_code(pg_session, user_id)

        claimed = enrollment.claim(pg_session, code=code, device_id=DEVICE, now=NOW)
        assert claimed is not None and claimed.user_id == user_id

    def test_the_second_claim_fails(self, pg_session, user_id):
        """**截图被别人拿到时**:要么你已经换过了(他换不了),
        要么你还没换(你会发现自己换不了)。两种都比"两个人各有一套"好。"""
        code, _ = a_code(pg_session, user_id)

        assert enrollment.claim(pg_session, code=code, device_id=DEVICE, now=NOW) is not None
        assert enrollment.claim(pg_session, code=code, device_id="another", now=NOW) is None

    def test_an_expired_code_fails(self, pg_session, user_id):
        """**在聊天记录里躺三天的码等于没有一次性。**"""
        code, _ = a_code(pg_session, user_id, ttl=timedelta(minutes=10))

        later = NOW + timedelta(hours=1)
        assert enrollment.claim(pg_session, code=code, device_id=DEVICE, now=later) is None

    def test_a_wrong_code_fails(self, pg_session, user_id):
        a_code(pg_session, user_id)
        assert enrollment.claim(pg_session, code="不是那个码", device_id=DEVICE, now=NOW) is None

    def test_the_claim_leaves_a_trace(self, pg_session, user_id):
        """**换过的码不删行。** "这台设备是什么时候、用哪个码配上的"是排查
        "我的号被别人配走了吗"唯一的线索。"""
        code, _ = a_code(pg_session, user_id)
        enrollment.claim(pg_session, code=code, device_id=DEVICE, now=NOW)

        (row,) = enrollment.list_codes(user_id, pg_session)
        assert row.claimed_at == NOW
        assert row.claimed_by == DEVICE

    def test_purging_keeps_the_used_ones(self, pg_session, user_id):
        used, _ = a_code(pg_session, user_id, ttl=timedelta(minutes=1))
        enrollment.claim(pg_session, code=used, device_id=DEVICE, now=NOW)
        a_code(pg_session, user_id, ttl=timedelta(minutes=1))  # 没用过的

        removed = enrollment.purge_expired(pg_session, cutoff=NOW + timedelta(hours=1))

        assert removed == 1
        assert len(enrollment.list_codes(user_id, pg_session)) == 1


class TestTheEndpoint:
    def claim(self, client, code: str, *, device_id: str = DEVICE):
        return client.post(
            "/enroll/claim",
            json={"code": code, "device_id": device_id, "app_version": "1.0.0"},
        )

    def test_it_needs_no_credentials(self, client, pg_session, user_id):
        """**这是唯一一个不需要凭据的写入口** —— 它换的就是凭据。"""
        code, _ = a_code(pg_session, user_id)

        response = self.claim(client, code)

        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == user_id
        assert body["device_id"] == DEVICE
        assert body["base_url"] == "https://lifein.example.com"
        assert body["collector_secret"] != body["query_secret"]

    def test_the_two_secrets_really_land_as_two_rows(self, client, pg_session, user_id):
        """铁律 12。**绝不是 `scope=both`** —— 那等于把"采集端只能写"作废。"""
        code, _ = a_code(pg_session, user_id)
        self.claim(client, code)

        rows = pg_session.execute(
            text(
                "SELECT kind, scope FROM credentials"
                " WHERE user_id = :u AND device_id = :d AND revoked_at IS NULL"
                " ORDER BY kind"
            ),
            {"u": user_id, "d": DEVICE},
        ).all()
        assert [(r.kind, r.scope) for r in rows] == [
            ("app_device", "query"),
            ("collector", "ingest"),
        ]

    def test_the_issued_secret_actually_works(self, client, pg_session, user_id):
        """**换完就能用。** 这一条钉住的是"配码之后 App 真的能连上",
        而那是整条流程唯一有意义的验收。"""
        code, _ = a_code(pg_session, user_id)
        secret = self.claim(client, code).json()["query_secret"]

        from tests.conftest import signed

        response = signed(
            client, "/app/token", {"device_id": DEVICE},
            user_id=user_id, secret=secret, device=DEVICE,
        )
        assert response.status_code == 200

    @pytest.mark.parametrize("attempt", ["wrong", "used", "expired"])
    def test_every_failure_looks_the_same(self, client, pg_session, user_id, attempt):
        """**码不对、用过了、过期了长得一模一样。** 区分等于告诉对方
        "这个码存在过",而那是爆破时唯一有用的信息。"""
        code, _ = a_code(pg_session, user_id, ttl=timedelta(minutes=10))
        if attempt == "used":
            self.claim(client, code)
        elif attempt == "expired":
            pg_session.execute(
                text("UPDATE enrollment_codes SET expires_at = :t WHERE user_id = :u"),
                {"t": NOW - timedelta(minutes=1), "u": user_id},
            )
        elif attempt == "wrong":
            code = "definitely-not-the-code"

        response = self.claim(client, code)
        assert response.status_code == 401
        assert response.text == ""

    def test_a_disabled_user_cannot_be_enrolled(self, client, pg_session, user_id):
        """码有效但用户被停用了。**码已经作废掉了,这是对的** ——
        一张指向不可用用户的码不该还能再试一次。"""
        code, _ = a_code(pg_session, user_id)
        pg_session.execute(
            text("UPDATE users SET disabled_at = now() WHERE id = :u"), {"u": user_id}
        )

        assert self.claim(client, code).status_code == 401
        assert enrollment.peek(pg_session, code=code).claimed_at is not None

    def test_the_device_id_comes_from_the_app(self, client, pg_session, user_id):
        """**人编的名字会重复**(两个人都叫 `phone`),而重复的 `device_id`
        意味着吊销一台会连带吊销另一台 —— 被吊销的那个人不知道发生了什么。"""
        code, _ = a_code(pg_session, user_id)

        response = self.claim(client, code, device_id="ab")  # 太短,像人手打的
        assert response.status_code == 422

    def test_only_the_asked_for_purpose_is_issued(self, client, pg_session, user_id):
        """朋友那台只采集不查询时,不该顺手给一把查询密钥。"""
        code, _ = a_code(pg_session, user_id, purpose="collect")

        body = self.claim(client, code).json()
        assert "collector_secret" in body
        assert "query_secret" not in body


def test_the_settings_fixture_is_the_shared_one():
    """夹具用的是 conftest 那份 —— 加密要用同一个 MASTER_KEY,
    否则换出来的密钥存进去之后自己解不开。"""
    assert api_settings().master_key is not None
