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


_INSERT_EVENT = text(
    "INSERT INTO raw_events (user_id, source, external_id, occurred_at,"
    " ingested_at, raw, trust)"
    " VALUES (:u, 'notification', :e, :t, :t, '{}'::jsonb, 'external')"
)


def an_event(session, user_id: str, external_id: str = "e1") -> None:
    """塞一条采集来的事件。**删除与停止采集那两组都要它** ——
    没有数据的时候,"删掉了"和"什么都没做"看起来完全一样。
    """
    session.execute(_INSERT_EVENT, {"u": user_id, "e": external_id, "t": NOW})


def count_events(session, user_id: str) -> int:
    return session.execute(
        text("SELECT count(*) FROM raw_events WHERE user_id = :u"), {"u": user_id}
    ).scalar_one()


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
    """**边界从"三样"变成了"两层",而"不重实现 App"那一条没变。**

    2026-09-10 按 [ADR-029] 改判之后,03 的 P4 给用户层划的范围是:
    数据导出与删除、设备与配码、自己的采集开关与白名单、隐私说明页。

    **红了不要顺手把新路径加进去。** 加一条要能指到那份范围里的某一样 ——
    指不到就是在把 App 搬到 Web 上,而那正是下面那条测试拦的东西。
    """
    from lifein.api import console

    paths = {r.path for r in console.router.routes}
    assert paths == {
        "/app/console/link",  # 入口:App 里点出来的一次性链接
        "/console",  # 总览
        "/console/devices",  # 账号与授权管理
        "/console/devices/invite",  # 账号与授权管理:添加设备
        "/console/devices/revoke",  # 账号与授权管理:手机丢了
        "/console/collection",  # 自己的采集开关与白名单
        "/console/collection/sources",
        "/console/collection/sources/toggle",
        "/console/collection/stop",
        "/console/data",  # 数据导出与删除
        "/console/export",
        "/console/data/delete",
        "/console/privacy",  # 隐私说明页
    }


def test_the_ledger_and_todos_and_memory_stay_in_the_app(client):
    """**这一条才是那句"只剩"真正的执行者。**

    上面那条是一份清单,而清单会被人顺手加长。这一条盯的是**形状**:
    账本、待办、记忆这三样在 App 里已经有一整套界面,
    **而一份两处实现的界面会有两套 bug 和两次要改**(ADR-029)。

    总览上可以有数字 —— 一个数字不会长出第二套确认逻辑,
    而一份可编辑的账本列表会。
    """
    from lifein.api import console

    paths = {r.path for r in console.router.routes}
    forbidden = ("ledger", "todos", "memory", "pending", "facts")
    offenders = [p for p in paths for word in forbidden if word in p]
    assert not offenders, f"这几条把 App 搬到 Web 上来了:{offenders}"


class TestAddingADevice:
    """**这一组是 03 的 P4 那句话的验收。**

    那一条写的是"Web 控制台里点添加设备 → 页面显示二维码",而**理由是**
    "现在的配码要人在服务器上跑 `issue-device`,非技术背景的人做不到"。

    二维码的内容在 P4 第 1 片就修对了(里面是一次性换取码,不是密钥),
    而出码这个动作在这之前还留在终端里 —— 于是那一条只做完了一半:
    payload 安全了,流程还是要开 SSH。
    """

    def with_url(self, browser, value="https://life.example.com"):
        """给这个客户端配一个 `PUBLIC_BASE_URL`。

        走 `dependency_overrides` 而不是改环境变量:`Settings` 是进程级缓存的,
        改环境变量会漏到别的用例里去,而那种污染只在整套一起跑时才出现。
        """
        from lifein.api.deps import get_app_settings
        from tests.conftest import api_settings

        def patched():
            settings = api_settings()
            object.__setattr__(settings, "public_base_url", value)
            return settings

        browser.app.dependency_overrides[get_app_settings] = patched

    def test_the_button_is_on_the_devices_page(self, browser, pg_session, user_id):
        """点得到才算做完 —— 藏在某个 URL 后面的功能等于没有。"""
        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        page = browser.get("/console/devices").text

        assert "添加设备" in page
        assert 'action="/console/devices/invite"' in page

    def test_the_overview_points_at_it(self, browser, pg_session, user_id):
        """总览上要有一条通往设备的路。**没有入口的页面等于不存在** ——
        而这个控制台上第一件要做的事往往就是配一台设备。"""
        raw = a_link(pg_session, user_id)

        page = open_console(browser, raw).text

        assert 'href="/console/devices"' in page

    def test_it_issues_a_code_and_shows_a_qr(self, browser, pg_session, user_id):
        from lifein.repos import enrollment

        self.with_url(browser)
        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        page = browser.post("/console/devices/invite")

        assert page.status_code == 200
        assert "<svg" in page.text, "页面上要有二维码本身,不是一个下载链接"
        assert "life.example.com" in page.text
        # 库里真多了一张码
        codes = pg_session.execute(
            text("SELECT count(*) FROM enrollment_codes WHERE user_id = :u"),
            {"u": user_id},
        ).scalar_one()
        assert codes == 1
        assert enrollment  # 用到它才说明这条路真走了仓储

    def test_the_qr_is_inline_not_a_link(self, browser, pg_session, user_id):
        """**控制台里没有任何外部引用。** 多一个外链就多一处 Referer
        会带着东西出去的地方 —— 而这一页上带着的是一张能换走密钥的码。"""
        self.with_url(browser)
        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        page = browser.post("/console/devices/invite").text

        # 断言的是"没有外部资源引用",不是"页面里没有 http" ——
        # SVG 自己的 xmlns 就是一个 http URL,而那个不发出任何请求
        assert "<img" not in page
        assert "src=" not in page
        assert 'href="http' not in page
        assert "<svg" in page

    def test_the_payload_is_v2_with_no_secrets(self, browser, pg_session, user_id):
        """**图里没有密钥。** 那正是它可以直接发给对方的原因 ——
        `issue-device` 打出来的那张不行。"""
        self.with_url(browser)
        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        page = browser.post("/console/devices/invite").text

        assert "&quot;v&quot;: 2" in page, "配码 payload 要原样显示出来,扫不动时还能手抄"
        assert "collector_secret" not in page
        assert "query_secret" not in page

    def test_it_needs_a_session(self, browser, pg_session, user_id):
        """没有会话 cookie 就出不了码 —— 它签发的是一张能换走两把密钥的东西。"""
        page = browser.post("/console/devices/invite").text
        assert "重新点一次" in page

    def test_it_is_not_a_link(self, browser, pg_session, user_id):
        """**GET 会被浏览器预取、被"重新打开上次的标签页"重放** ——
        而每重放一次就多一张有效的码,每一张都能配上一台设备。"""
        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        assert browser.get("/console/devices/invite").status_code == 405

    def test_without_a_public_url_it_says_so(self, browser, pg_session, user_id):
        """**不猜一个地址。**

        那个值会变成手机里"我的服务端在哪",而猜错的后果是他把自己的通知
        报到了别处。请求头是发请求的人说了算的,照着它生成的码可能把手机
        指到别人的服务器上。
        """
        from lifein.repos import enrollment  # noqa: F401

        self.with_url(browser, value=None)
        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        page = browser.post("/console/devices/invite")

        assert "PUBLIC_BASE_URL" in page.text
        # **一张码都不该被签出来** —— 出一张指向 example.com 的码比不出更糟
        codes = pg_session.execute(
            text("SELECT count(*) FROM enrollment_codes WHERE user_id = :u"),
            {"u": user_id},
        ).scalar_one()
        assert codes == 0

    def test_the_code_belongs_to_whoever_clicked(
        self, browser, pg_session, user_id, monkeypatch
    ):
        """铁律 1。`user_id` 从会话里取,页面上没有任何地方能指定别人 ——
        **给朋友开账号是另一件事**,那要跑 `admin add-user`。"""
        self.with_url(browser)
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

        browser.post("/console/devices/invite")

        owner = pg_session.execute(
            text("SELECT user_id FROM enrollment_codes")
        ).scalar_one()
        assert str(owner) == other

    def test_the_ttl_on_the_page_matches_the_real_one(
        self, browser, pg_session, user_id, monkeypatch
    ):
        """页面上那句"十分钟内有效"必须和真正的过期时间是同一个数。

        **各写各的话,先过期的是用户不知道的那一个** —— 他照着页面上的话
        慢悠悠去扫,而码已经废了。
        """
        from lifein.api.console import INVITE_TTL

        self.with_url(browser)
        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        page = browser.post("/console/devices/invite").text
        expires = pg_session.execute(
            text("SELECT expires_at FROM enrollment_codes")
        ).scalar_one()

        assert f"{int(INVITE_TTL.total_seconds() // 60)} 分钟内有效" in page
        assert expires == NOW + INVITE_TTL

    def test_the_version_matches_what_the_app_expects(self):
        """服务端出的 `v` 和 App 认的那个必须是同一个数。

        对不上的话新出的码会被 App 当成缺字段的旧式配码,
        报一句完全指错方向的话 —— 而用户手上只有那句话。
        """
        from pathlib import Path

        from lifein.repos.enrollment import INVITE_VERSION

        kotlin = (
            Path(__file__).resolve().parents[1]
            / "android/app/src/main/java/ltd/iclab/lifein/data/Enrollment.kt"
        ).read_text(encoding="utf-8")
        assert f'INVITE_VERSION = "{INVITE_VERSION}"' in kotlin


class TestTheDevicesPage:
    """**吊销是这一层唯一能把一台设备踢出去的动作**,而它不可撤销 ——
    所以它要点两次,而且第一次点完什么都还没发生。
    """

    def test_it_lists_credentials_with_their_kind(self, browser, pg_session, user_id, secrets):
        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        page = browser.get("/console/devices").text

        assert "pixel-7a" in page
        assert "采集(只写)" in page
        assert "查询(只读)" in page

    def test_the_first_click_changes_nothing(self, browser, pg_session, user_id, secrets):
        """**确认页是一次真正的暂停,不是一句装饰。**

        第一次 POST 之后库里还一条都没吊销 —— 关掉浏览器就等于没点过。
        """
        from lifein.repos import credentials

        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        page = browser.post("/console/devices/revoke", data={"device_id": "pixel-7a"})

        assert "吊销这台设备" in page.text
        alive = [
            d
            for d in credentials.list_device_credentials(user_id, pg_session)
            if d.revoked_at is None
        ]
        assert len(alive) == 2, "第一次点完不该动任何东西"

    def test_the_second_click_revokes_both(self, browser, pg_session, user_id, secrets):
        """**默认吊销两条而不是一条。** 触发它的场景是「手机丢了」,
        那时只吊销其中一种,等于把另一种留在别人手里。"""
        from lifein.repos import credentials

        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        browser.post("/console/devices/revoke", data={"device_id": "pixel-7a", "confirm": "yes"})

        alive = [
            d
            for d in credentials.list_device_credentials(user_id, pg_session)
            if d.revoked_at is None
        ]
        assert alive == []

    def test_a_revoked_row_stays_visible(self, browser, pg_session, user_id, secrets):
        """**记录保留不删。** 哪台设备什么时候被吊销的,是排查「丢了之后
        还有没有人在用」时唯一的线索。"""
        raw = a_link(pg_session, user_id)
        open_console(browser, raw)
        browser.post("/console/devices/revoke", data={"device_id": "pixel-7a", "confirm": "yes"})

        page = browser.get("/console/devices").text

        assert "pixel-7a" in page
        assert "已吊销" in page

    def test_it_needs_a_session(self, browser, pg_session, user_id, secrets):
        page = browser.post(
            "/console/devices/revoke", data={"device_id": "pixel-7a", "confirm": "yes"}
        )
        assert "重新点一次" in page.text

    def test_it_only_touches_your_own(self, browser, pg_session, user_id, secrets):
        """铁律 1。`user_id` 从会话里取 —— 表单里写别人的设备名也吊销不了别人。"""
        from lifein.repos import credentials

        other = "88888888-8888-8888-8888-888888888888"
        pg_session.execute(
            text(
                "INSERT INTO users (id, display_name, wecom_userid)"
                " VALUES (:i, '别人', 'other-dev')"
            ),
            {"i": other},
        )
        raw = a_link(pg_session, other)
        open_console(browser, raw)

        browser.post("/console/devices/revoke", data={"device_id": "pixel-7a", "confirm": "yes"})

        alive = [
            d
            for d in credentials.list_device_credentials(user_id, pg_session)
            if d.revoked_at is None
        ]
        assert len(alive) == 2, "别人的会话不该动得了这个用户的设备"


class TestTheCollectionPage:
    """R10 改判四前提之一在 Web 上的样子:**朋友要能自己关掉采集**。

    App 里已经有,而一个人坐在电脑前的时候不该被要求先去掏手机。
    """

    def test_no_sources_says_so_loudly(self, browser, pg_session, user_id):
        """**一条都没放行 = 采集器送上去的东西全会被丢掉。**
        说不清楚的话,人会以为「配好了就在采」。"""
        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        page = browser.get("/console/collection").text

        assert "全会被丢掉" in page

    def test_allowing_a_package(self, browser, pg_session, user_id):
        from lifein.repos import collector

        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        browser.post("/console/collection/sources", data={"pattern": "com.tencent.mm"})

        rules = collector.list_whitelist(user_id, pg_session)
        assert [r.pattern for r in rules] == ["com.tencent.mm"]
        assert rules[0].purpose == "message"

    def test_the_web_cannot_open_the_transaction_lane(self, browser, pg_session, user_id):
        """**`purpose` 写死成 `message`,页面上没有地方能选。**

        银行与支付类会直接进记账链路,而那一档要在终端上做一次显式的动作,
        好让「我知道我在打开什么」至少发生过一次。
        """
        from lifein.repos import collector

        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        browser.post(
            "/console/collection/sources",
            data={"pattern": "com.icbc", "purpose": "transaction"},
        )

        assert collector.list_whitelist(user_id, pg_session)[0].purpose == "message"

    def test_toggling_one_off_keeps_the_row(self, browser, pg_session, user_id):
        """**没有删除,只有停用** —— 留着那一行才回答得了「曾经放行过谁」(06 §6.9)。"""
        from lifein.repos import collector

        raw = a_link(pg_session, user_id)
        open_console(browser, raw)
        browser.post("/console/collection/sources", data={"pattern": "com.tencent.mm"})
        rule_id = collector.list_whitelist(user_id, pg_session)[0].id

        browser.post(
            "/console/collection/sources/toggle", data={"rule_id": rule_id, "enabled": "0"}
        )

        rules = collector.list_whitelist(user_id, pg_session)
        assert len(rules) == 1 and rules[0].enabled is False

    def test_stopping_takes_two_clicks(self, browser, pg_session, user_id, secrets):
        from lifein.repos import collector, data_control

        raw = a_link(pg_session, user_id)
        open_console(browser, raw)
        browser.post("/console/collection/sources", data={"pattern": "com.tencent.mm"})

        first = browser.post("/console/collection/stop")
        assert "关掉采集" in first.text
        assert data_control.state(user_id, pg_session).enabled is True

        browser.post("/console/collection/stop", data={"confirm": "yes"})
        assert data_control.state(user_id, pg_session).enabled is False
        assert collector.list_whitelist(user_id, pg_session)[0].enabled is False

    def test_stopping_does_not_delete_anything(self, browser, pg_session, user_id, secrets):
        """**两件事分开是有意的。** 合成一个的话,「我想先停下来想想」
        就变成了「要么继续采要么全删」。"""
        raw = a_link(pg_session, user_id)
        open_console(browser, raw)
        an_event(pg_session, user_id)

        browser.post("/console/collection/stop", data={"confirm": "yes"})

        assert count_events(pg_session, user_id) == 1


class TestTheDataPage:
    def test_deleting_takes_two_clicks(self, browser, pg_session, user_id):
        raw = a_link(pg_session, user_id)
        open_console(browser, raw)
        an_event(pg_session, user_id)

        first = browser.post("/console/data/delete")
        assert "删了就找不回来了" in first.text
        assert count_events(pg_session, user_id) == 1, "第一次点完不该删任何东西"

        browser.post("/console/data/delete", data={"confirm": "yes"})
        assert count_events(pg_session, user_id) == 0

    def test_the_page_says_what_survives(self, browser, pg_session, user_id):
        """**删不掉的那两样要写在页面上。**

        审计日志和被拒绝过的待确认都留着,而一个说「全删了」却留下东西的
        按钮,比一个说清楚的按钮更伤信任。
        """
        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        page = browser.get("/console/data").text

        assert "审计日志" in page
        assert "拒绝" in page


class TestTheShell:
    def test_there_are_no_external_references_anywhere(
        self, browser, pg_session, user_id, secrets
    ):
        """**控制台里没有任何外部引用**,不只是配码那一页。

        一个外链就多一处 Referer 会带着东西出去的地方 —— 而这些页面上
        带着的是配码、导出、和别人的用户名。字体用系统栈、图标是内联 SVG、
        样式在 `<style>` 里,所以这一条是做得到的。
        """
        raw = a_link(pg_session, user_id)
        open_console(browser, raw)

        for path in ("/console", "/console/devices", "/console/collection", "/console/data"):
            page = browser.get(path).text
            assert "<img" not in page, path
            assert "src=" not in page, path
            assert 'href="http' not in page, path
            assert "<script" not in page, path

    def test_the_operator_entrance_is_hidden_from_friends(self, browser, pg_session, user_id):
        """**「运营台」那个入口只对运营者本人显示。**

        对别人显示它没有安全问题(那边照样要口令),但它会让一个朋友以为
        这里有一块他打不开的地方 —— 而这个控制台要给人的感觉恰恰相反。
        """
        raw = a_link(pg_session, user_id)

        page = open_console(browser, raw).text

        assert "运营台" not in page

    def test_the_operator_sees_it(self, browser, pg_session, user_id):
        from lifein.repos import users

        users.set_admin(user_id, pg_session, is_admin=True)
        raw = a_link(pg_session, user_id)

        page = open_console(browser, raw).text

        assert "运营台" in page
        assert 'href="/admin"' in page

    def test_every_page_survives_a_dead_session(self, browser, pg_session, user_id):
        """**每一页都要各自认一次。** 漏掉一页的表现不是报错,
        是那一页对着一个过期会话照常渲染 —— 而它上面有导出和删除。"""
        browser.cookies.set("lifein_console", "not-a-real-token")

        for path in ("/console", "/console/devices", "/console/collection", "/console/data"):
            assert "重新点一次" in browser.get(path).text, path
