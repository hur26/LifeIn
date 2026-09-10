"""运营层控制台的口令与会话(ADR-029)。**不需要数据库** —— 这一层没有库。

它测的是三件事,每一件都对应一条"错了不会有任何报错"的性质:

- 口令的派生值放进 `.env` 之后还认得出来,而**换参数不会让已配好的失效**
- 会话是签出来的,伪造一张改过期时间的进不来
- 失败闸门会落下,而且到点自己开
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lifein.console_auth import (
    LoginGate,
    hash_password,
    issue_session,
    verify_password,
    verify_session,
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings():
    """会话签名从主密钥派生,所以这一层要的只有那一把。"""
    from tests.conftest import api_settings

    return api_settings()


class TestThePassword:
    def test_it_round_trips(self):
        encoded = hash_password("一个足够长的口令 abc")

        assert verify_password("一个足够长的口令 abc", encoded)
        assert not verify_password("一个足够长的口令 abd", encoded)

    def test_the_plaintext_is_not_in_there(self):
        """**这一行会被抄进 `.env`,而 `.env` 会跟着磁盘快照走**(ADR-022)。"""
        encoded = hash_password("correct horse battery staple")

        assert "correct" not in encoded
        assert "horse" not in encoded

    def test_two_hashes_of_the_same_password_differ(self):
        """每次一把新盐。一样的话,两个人用同一个口令在 `.env` 里长得一模一样 ——
        而那件事会被看见(两台机器的配置摆在一起时)。"""
        assert hash_password("同一个口令 xxxxxx") != hash_password("同一个口令 xxxxxx")

    def test_the_parameters_travel_with_the_hash(self):
        """**参数写在编码里,不是写死在代码里。**

        调大参数那天,已经配好的那些还要能校验通过 ——
        否则一次安全加固会变成一次谁也登不进去。
        """
        encoded = hash_password("参数要跟着走 abcdef")
        scheme, n, r, p, _salt, _dk = encoded.split("$")

        assert scheme == "scrypt"
        assert (int(n), int(r), int(p)) == (1 << 15, 8, 1)

    @pytest.mark.parametrize(
        "garbage",
        ["", "不是哈希", "scrypt$32768$8$1$只有五段", "bcrypt$1$2$3$4$5"],
    )
    def test_a_broken_hash_just_fails(self, garbage):
        """**一个配错了的哈希和一个错的口令,对着登录页的人看到的是同一句话。**

        "格式不对"那句会告诉他这台机器上运营层是配过的。
        """
        assert verify_password("随便什么", garbage) is False


class TestTheSession:
    def test_a_fresh_one_verifies(self, settings):
        token = issue_session(settings=settings, now=NOW, ttl=timedelta(hours=2))

        assert verify_session(token, settings=settings, now=NOW)

    def test_it_expires(self, settings):
        token = issue_session(settings=settings, now=NOW, ttl=timedelta(hours=2))

        assert not verify_session(token, settings=settings, now=NOW + timedelta(hours=3))

    def test_it_carries_no_user_id(self, settings):
        """运营者是谁由 `users.is_admin` 那一行回答。写进 cookie 的话,
        改那一行之后旧 cookie 还指着旧的人。"""
        token = issue_session(settings=settings, now=NOW, ttl=timedelta(hours=2))

        assert token.count(".") == 3  # 版本、过期、随机数、签名 —— 没有第五段

    def test_a_forged_expiry_does_not_get_in(self, settings):
        """**这是这个模块存在的理由。** 过期时间在明文里,任何人都改得动 ——
        挡住它的只有签名。"""
        token = issue_session(settings=settings, now=NOW, ttl=timedelta(hours=2))
        version, _expires, nonce, signature = token.split(".")
        far_future = int((NOW + timedelta(days=365)).timestamp())

        forged = f"{version}.{far_future}.{nonce}.{signature}"

        assert not verify_session(forged, settings=settings, now=NOW + timedelta(days=1))

    def test_another_master_key_cannot_sign_one(self, settings):
        """签名密钥从主密钥派生。换了主密钥,旧会话全部作废 ——
        **而那正是轮换应该有的效果**。"""
        import base64

        from tests.conftest import api_settings

        token = issue_session(settings=settings, now=NOW, ttl=timedelta(hours=2))
        other = api_settings()
        object.__setattr__(
            other,
            "master_key",
            type(settings.master_key)(base64.b64encode(b"B" * 32).decode()),
        )

        assert not verify_session(token, settings=other, now=NOW)

    @pytest.mark.parametrize("garbage", [None, "", "不是会话", "v1.1.2", "v2.1.2.3"])
    def test_garbage_is_just_false(self, settings, garbage):
        assert verify_session(garbage, settings=settings, now=NOW) is False


class TestTheGate:
    def test_it_stays_open_below_the_limit(self):
        gate = LoginGate(max_attempts=3, lockout=timedelta(minutes=15))

        gate.record_failure(NOW)
        gate.record_failure(NOW)

        assert gate.blocked(NOW) is None

    def test_it_shuts_at_the_limit(self):
        gate = LoginGate(max_attempts=3, lockout=timedelta(minutes=15))

        for _ in range(3):
            gate.record_failure(NOW)

        assert gate.blocked(NOW) == timedelta(minutes=15)

    def test_it_opens_again_on_its_own(self):
        """**到点自己开。** 一个人敲错口令时可以把自己锁在门外,
        而那个代价只有在它会自己解除时才是可以接受的。"""
        gate = LoginGate(max_attempts=1, lockout=timedelta(minutes=15))
        gate.record_failure(NOW)

        assert gate.blocked(NOW + timedelta(minutes=16)) is None

    def test_reopening_clears_the_count(self):
        """留着计数的话,锁一开下一次失败会立刻再锁上 ——
        表现是"等了十五分钟,敲错一次又要再等十五分钟"。"""
        gate = LoginGate(max_attempts=2, lockout=timedelta(minutes=15))
        gate.record_failure(NOW)
        gate.record_failure(NOW)
        gate.blocked(NOW + timedelta(minutes=16))

        gate.record_failure(NOW + timedelta(minutes=16))

        assert gate.blocked(NOW + timedelta(minutes=16)) is None

    def test_a_success_clears_it(self):
        gate = LoginGate(max_attempts=3, lockout=timedelta(minutes=15))
        gate.record_failure(NOW)

        gate.record_success()

        assert gate.failures == 0
