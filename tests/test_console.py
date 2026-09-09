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


@pytest.fixture
def browser(client):
    """一个**走 https 的**测试客户端。

    会话 cookie 带着 `Secure`,所以 http 下浏览器根本不会存它 ——
    而那不是测试的麻烦,是设计:控制台必须在 TLS 后面(和 App 那边
    "base_url 必须是 https"同一条)。为本机调试放开 `Secure`,
    等于让线上那份也少一道。

    **这个夹具本身就是那条要求的执行者:** 谁把 `secure=True` 去掉,
    这里不会红 —— 但谁把它改成"按环境变量决定",下一个人就会在 http 上跑通,
    然后以为没问题。
    """
    client.base_url = "https://testserver"
    return client


def open_console(browser, token: str):
    """按人的方式进控制台:带 token 进门,换 cookie,落到干净的 URL。"""
    return browser.get(f"/console?t={token}", follow_redirects=True)


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

        assert console_links.resolve(pg_session, token=raw, now=NOW).user_id == user_id
        assert console_links.resolve(pg_session, token=raw, now=NOW).user_id == user_id

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
    def test_the_home_page_lists_your_devices(self, browser, pg_session, user_id, secrets):
        raw = a_link(pg_session, user_id)

        page = open_console(browser, raw).text
        assert "你的设备" in page
        assert "pixel-7a" in page

    def test_the_token_leaves_the_url_at_the_door(self, browser, pg_session, user_id):
        """**token 只在门口出现一次。**

        原来页面里每个链接都带着它,于是那串东西在整个会话里反复出现 ——
        进浏览器历史、进反代日志、可能进 Referer。而导出那一条下下来的
        是全部个人数据:任何拿到那行历史的人,十五分钟内点一下就有。
        """
        raw = a_link(pg_session, user_id)

        landed = open_console(browser, raw)

        assert str(landed.url).endswith("/console")
        assert raw not in str(landed.url)
        assert raw not in landed.text, "页面里一个链接都不该再带 token"

    def test_the_session_cookie_is_locked_down(self, browser, pg_session, user_id):
        """四样都不是可选的。少一样各自漏一条路:

        - `HttpOnly`:页面脚本读得到它
        - `Secure`:明文网络上看得到它
        - `SameSite=Strict`:别的站点点过来时会带上
        - `Path=/console`:别的接口也拿得到它
        """
        raw = a_link(pg_session, user_id)

        response = browser.get(f"/console?t={raw}", follow_redirects=False)

        assert response.status_code == 303
        header = response.headers["set-cookie"]
        assert "HttpOnly" in header
        assert "Secure" in header
        assert "SameSite=strict" in header.replace("samesite", "SameSite")
        assert "Path=/console" in header

    def test_the_cookie_does_not_outlive_the_link(self, browser, pg_session, user_id):
        """**活得比 token 长的 cookie 是一个看起来还能用、实际已作废的会话** ——
        表现是点导出之后跳回首页说"链接过期了",而地址栏什么都没变。"""
        raw = a_link(pg_session, user_id, ttl=timedelta(minutes=5))

        header = browser.get(
            f"/console?t={raw}", follow_redirects=False
        ).headers["set-cookie"]

        assert "Max-Age=300" in header

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

    def test_the_export_downloads(self, browser, pg_session, user_id):
        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        response = browser.post("/console/export")

        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        assert "credentials" not in response.json()["tables"]

    def test_the_export_is_not_a_link(self, browser, pg_session, user_id):
        """**下载一份全部个人数据不该是一个能被顺手重放的动作。**

        GET 会被浏览器预取、被 Referer 带走、被"重新打开上次的标签页"重放。
        一个表单按钮和一个链接在用户眼里没有区别,而在这几件事上差很远。
        """
        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        assert browser.get("/console/export").status_code == 405

    def test_the_export_ignores_a_token_in_the_url(self, browser, pg_session, user_id):
        """**只认 cookie。** 留着 query 那条路等于上面那些都白改。"""
        raw = a_link(pg_session, user_id)

        page = browser.post(f"/console/export?t={raw}").text
        assert "重新点一次" in page

    def test_the_export_needs_a_valid_link(self, browser, pg_session, user_id):
        """**导出是这个控制台上最该拦住的动作** —— 它一次给出全部内容。"""
        # cookie 只能放 ASCII,所以这里用一串没人认得的 token
        browser.cookies.set("lifein_console", "not-a-real-token")
        page = browser.post("/console/export").text
        assert "重新点一次" in page

    def test_someone_elses_link_shows_their_own_data_only(
        self, browser, pg_session, user_id
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
        open_console(browser, raw)

        body = browser.post("/console/export").json()
        assert body["user_id"] == other


def test_the_console_does_not_reimplement_the_app(client):
    """**03 那句"只剩"是这一片的边界。** 账本、待办、记忆都不进来 ——
    App 已经有了,而一份两处实现的界面会有两套 bug 和两次要改。
    """
    from lifein.api import console

    paths = {r.path for r in console.router.routes}
    assert paths == {"/app/console/link", "/console", "/console/export", "/console/privacy"}
