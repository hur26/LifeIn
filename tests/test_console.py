"""Web 控制台(P4 第 9 片)。需要真实 PostgreSQL。

03 说 Web 只剩"数据导出、账号与授权管理、隐私说明页"这些合规必需项 ——
所以这一组也只测那三件,加上它们共同的那个真实设计问题:

**浏览器打开一个链接时带不了 `Authorization` 头。** App 里那套 Bearer token
在 Web 上直接用不了,所以有了一次性链接。这一组大半在测它:
过期的进不去、别人的进不去、而过期之后看到的是"再点一次"不是报错页。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from lifein.repos import console_links
from tests.conftest import NOW, bearer

pytestmark = pytest.mark.integration


def a_link(session, user_id, *, ttl=timedelta(minutes=15)) -> str:
    token, _ = console_links.issue(user_id, session, now=NOW, ttl=ttl)
    return token


class TestTheLink:
    def test_the_app_can_mint_one(self, client, pg_session, user_id, token):
        """**这是控制台唯一的入口** —— 没有登录页,也没有密码。"""
        body = client.post("/app/console/link", headers=bearer(token)).json()

        assert body["url"].startswith("/console?t=")
        assert client.get(body["url"]).status_code == 200

    def test_minting_one_needs_a_token(self, client, pg_session, user_id):
        assert client.post("/app/console/link").status_code == 401

    def test_the_plaintext_is_not_stored(self, pg_session, user_id):
        """和配码那张码一样:库被拖走时,里面的东西不该直接可用。"""
        raw = a_link(pg_session, user_id)

        stored = pg_session.execute(
            text("SELECT token_hash FROM console_links WHERE user_id = :u"), {"u": user_id}
        ).scalar_one()
        assert raw not in stored

    def test_it_stays_usable_within_its_window(self, pg_session, user_id):
        """**它是短命的多次可用,不是一次性的。** 页面上有链接要点,
        每点一次就换一张的话,每次点击都要回 App 一趟。"""
        raw = a_link(pg_session, user_id)

        assert console_links.resolve(pg_session, token=raw, now=NOW) == user_id
        assert console_links.resolve(pg_session, token=raw, now=NOW) == user_id

    def test_an_expired_one_resolves_to_nothing(self, pg_session, user_id):
        raw = a_link(pg_session, user_id, ttl=timedelta(minutes=1))

        later = NOW + timedelta(hours=1)
        assert console_links.resolve(pg_session, token=raw, now=later) is None

    def test_purging_removes_expired_ones(self, pg_session, user_id):
        """**这里可以真删。** 和配码那张不一样:那张要留"谁什么时候配上的"
        的痕迹,而"某人打开过控制台"不是那种事。"""
        a_link(pg_session, user_id, ttl=timedelta(minutes=1))

        assert console_links.purge_expired(pg_session, cutoff=NOW + timedelta(hours=1)) == 1


class TestThePages:
    def test_the_home_page_lists_your_devices(self, client, pg_session, user_id, secrets):
        raw = a_link(pg_session, user_id)

        page = client.get(f"/console?t={raw}").text
        assert "你的设备" in page
        assert "pixel-7a" in page

    def test_a_bad_link_says_click_again_not_error(self, client, pg_session, user_id):
        """**过期的链接不是错误,是"再点一次"** —— 报错页会让人以为
        自己做错了什么。"""
        page = client.get("/console?t=不是那个").text

        assert "重新点一次" in page
        assert "隐私说明" in page  # 而且仍然能读隐私说明

    def test_no_link_at_all_is_the_same_page(self, client):
        assert client.get("/console").status_code == 200

    def test_the_privacy_page_needs_no_auth(self, client):
        """**一份要先登录才能看的隐私说明,等于没有隐私说明** ——
        接入之前的人恰恰是最该读它的那个。"""
        response = client.get("/console/privacy")

        assert response.status_code == 200
        assert "白杨在技术上读得到" in response.text

    def test_the_export_downloads(self, client, pg_session, user_id):
        raw = a_link(pg_session, user_id)

        response = client.get(f"/console/export?t={raw}")

        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        assert "credentials" not in response.json()["tables"]

    def test_the_export_needs_a_valid_link(self, client, pg_session, user_id):
        """**导出是这个控制台上最该拦住的动作** —— 它一次给出全部内容。"""
        page = client.get("/console/export?t=过期的").text
        assert "重新点一次" in page

    def test_someone_elses_link_shows_their_own_data_only(
        self, client, pg_session, user_id
    ):
        """铁律 1。链接换出来的是那张链接的主人,不是请求里说的任何人。"""
        other = "99999999-9999-9999-9999-999999999999"
        pg_session.execute(
            text(
                "INSERT INTO users (id, display_name, wecom_userid)"
                " VALUES (:i, '别人', 'other')"
            ),
            {"i": other},
        )
        raw = a_link(pg_session, other)

        body = client.get(f"/console/export?t={raw}").json()
        assert body["user_id"] == other


def test_the_console_does_not_reimplement_the_app(client):
    """**03 那句"只剩"是这一片的边界。** 账本、待办、记忆都不进来 ——
    App 已经有了,而一份两处实现的界面会有两套 bug 和两次要改。
    """
    from lifein.api import console

    paths = {r.path for r in console.router.routes}
    assert paths == {"/app/console/link", "/console", "/console/export", "/console/privacy"}
