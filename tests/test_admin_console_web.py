"""控制台的**运营层**(ADR-029)。需要真实 PostgreSQL。

这一组分两半:

- **门**:没配口令时整层不存在、错口令进不去、失败太多次闸门落下、
  伪造一张 cookie 也进不去
- **边界**:注销用户、导出别人的数据、配凭据 —— 三样都**刻意留在终端里**,
  这里一条路径都不该有

那三样不是漏做的。往里加东西之前先问一句:它属于「每天要看一眼」,
还是属于「一年做一次而且做错了很贵」?后者留在终端里。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from lifein.api.deps import get_app_settings
from lifein.console_auth import hash_password
from lifein.repos import users
from tests.conftest import NOW, api_settings

pytestmark = pytest.mark.integration

PASSWORD = "一个够长的运营口令"


@pytest.fixture
def admin(client):
    """一个**走 https 的**客户端,而且服务端配好了运营层口令。

    `Secure` 不是可选的:控制台必须在 TLS 后面。为本机调试放开它,
    等于让线上那份也少一道(和用户层那个 `browser` 夹具同一条)。
    """
    client.base_url = "https://testserver"
    _with_password(client, hash_password(PASSWORD))
    return client


def _with_password(client, encoded: str | None):
    """给这个客户端配(或不配)运营层口令。

    走 `dependency_overrides` 而不是改环境变量:`Settings` 是进程级缓存的,
    改环境变量会漏到别的用例里去,而那种污染只在整套一起跑时才出现。
    """

    def patched():
        settings = api_settings()
        object.__setattr__(settings, "console_admin_password_hash", encoded)
        return settings

    client.app.dependency_overrides[get_app_settings] = patched


def sign_in(admin, password: str = PASSWORD):
    return admin.post("/admin/login", data={"password": password}, follow_redirects=False)


@pytest.fixture(autouse=True)
def open_gate():
    """每个用例开始前把闸门抬起来。

    **它是进程级的**(一个运营者,不按 IP 分),所以上一个用例敲错的口令
    会把下一个用例锁在门外 —— 而那种失败看起来像功能坏了。
    """
    from lifein.api import admin_console

    admin_console._GATE.record_success()
    yield
    admin_console._GATE.record_success()


class TestTheDoor:
    def test_without_a_password_the_layer_does_not_exist(self, client):
        """**留空 = 这一层整个不存在**,而不是「这一层不设防」(07 §2.8)。"""
        client.base_url = "https://testserver"
        _with_password(client, None)

        page = client.get("/admin").text

        assert "没有运营台" in page
        assert "CONSOLE_ADMIN_PASSWORD_HASH" in page

    def test_without_a_password_login_gets_you_nowhere(self, client):
        """**不存在的认证面是攻不破的** —— 但它得真的攻不破,
        而不是「登录页不显示、POST 照收」。"""
        client.base_url = "https://testserver"
        _with_password(client, None)

        response = client.post("/admin/login", data={"password": ""}, follow_redirects=False)

        assert "set-cookie" not in response.headers

    def test_the_front_door_is_a_login_page(self, admin):
        page = admin.get("/admin").text

        assert "运营台" in page
        assert 'action="/admin/login"' in page
        # **没有导航。** 一份列着「用户」「运维」的侧边栏,对着还没进门的人,
        # 等于把这台机器上有什么先说了一遍
        assert 'href="/admin/users"' not in page

    def test_a_wrong_password_gets_no_cookie(self, admin):
        response = sign_in(admin, "错的口令错的口令")

        assert "set-cookie" not in response.headers
        assert "口令不对" in response.text

    def test_the_right_password_gets_a_locked_down_cookie(self, admin):
        """四样都不是可选的,和用户层那张同一条。多一样:`Path=/admin` ——
        **两层在浏览器里从不互相看见。**"""
        response = sign_in(admin)

        assert response.status_code == 303
        header = response.headers["set-cookie"]
        assert "HttpOnly" in header
        assert "Secure" in header
        assert "SameSite=strict" in header.replace("samesite", "SameSite")
        assert "Path=/admin" in header

    def test_signing_in_gets_you_the_overview(self, admin, pg_session, user_id):
        sign_in(admin)

        page = admin.get("/admin").text

        assert "总览" in page
        assert "测试用户" in page

    def test_a_forged_cookie_does_not_get_in(self, admin, pg_session, user_id):
        """**过期时间在 cookie 的明文里,任何人都改得动** —— 挡住它的只有签名。"""
        admin.cookies.set("lifein_admin", "v1.99999999999.AAAA.AAAA")

        assert "action=\"/admin/login\"" in admin.get("/admin").text

    def test_every_page_needs_the_session(self, admin, pg_session, user_id):
        """**每一页都要各自认一次。** 漏掉一页的表现不是报错,
        是那一页对着一个没登录的人照常渲染 —— 而它上面是别人的数据。"""
        for path in ("/admin", "/admin/users", "/admin/ops", f"/admin/users/{user_id}"):
            assert 'action="/admin/login"' in admin.get(path).text, path

    def test_the_gate_shuts_after_too_many_tries(self, admin):
        """**真正的防线是 scrypt 那几十毫秒**,闸门只是让那几十毫秒
        不必被付上一万次。"""
        for _ in range(5):
            sign_in(admin, "错的口令错的口令")

        response = sign_in(admin)  # 这次口令是对的

        assert "set-cookie" not in response.headers
        assert "失败太多次" in response.text

    def test_signing_out_drops_the_cookie(self, admin, pg_session, user_id):
        sign_in(admin)

        response = admin.post("/admin/logout", follow_redirects=False)

        assert 'lifein_admin=""' in response.headers["set-cookie"]


class TestTheBoundary:
    """**三样刻意留在终端里的东西**,这里一条路径都不该有。"""

    def test_there_is_no_way_to_purge_a_user(self):
        """它擦掉一个人的全部存在,而且不可撤销 ——
        **一个能擦掉一个人的按钮,该要求你先打开终端。**"""
        from lifein.api import admin_console

        paths = {r.path for r in admin_console.router.routes}
        assert not [p for p in paths if "purge" in p or "delete" in p]

    def test_there_is_no_way_to_export_someone_elses_data(self):
        """本人在自己的控制台上点一下就有。运营者要替他导,
        那是一次需要解释的动作,不该做成一个随手能点的按钮。"""
        from lifein.api import admin_console

        paths = {r.path for r in admin_console.router.routes}
        assert not [p for p in paths if "export" in p]

    def test_there_is_no_way_to_set_credentials(self):
        """授权码一律从交互输入读 —— 而一个 HTTP 表单会进反代日志。"""
        from lifein.api import admin_console

        paths = {r.path for r in admin_console.router.routes}
        assert not [p for p in paths if "imap" in p or "weixin" in p or "credential" in p]

    def test_the_whitelist_is_read_only_here(self, admin, pg_session, user_id):
        """**白名单是他自己的东西。** 运营者替他放行一个包名,
        等于替他决定采什么 —— 那要他自己点。"""
        from lifein.api import admin_console

        paths = {r.path for r in admin_console.router.routes}
        assert not [p for p in paths if "whitelist" in p or "sources" in p]


class TestManagingAccounts:
    def test_disabling_takes_two_clicks(self, admin, pg_session, user_id):
        sign_in(admin)

        first = admin.post(f"/admin/users/{user_id}/disabled", data={"disabled": "1"})
        assert "停用" in first.text
        assert users.get_user(user_id, pg_session).active is True, "第一次点完不该动"

        admin.post(
            f"/admin/users/{user_id}/disabled", data={"disabled": "1", "confirm": "yes"}
        )
        assert users.get_user(user_id, pg_session).active is False

    def test_restoring_does_not(self, admin, pg_session, user_id):
        """**不对称是有意的。** 要一个人对无害的动作再确认一次,
        只会训练他闭着眼点确认。"""
        sign_in(admin)
        users.set_disabled(user_id, pg_session, disabled_at=NOW)

        admin.post(f"/admin/users/{user_id}/disabled", data={"disabled": "0"})

        assert users.get_user(user_id, pg_session).active is True

    def test_disabling_leaves_the_data_and_the_devices(
        self, admin, pg_session, user_id, secrets
    ):
        """**「先别再花钱扫他的邮箱」和「这台手机丢了」不是同一件事。**"""
        from lifein.repos import credentials

        sign_in(admin)

        admin.post(
            f"/admin/users/{user_id}/disabled", data={"disabled": "1", "confirm": "yes"}
        )

        alive = [
            d
            for d in credentials.list_device_credentials(user_id, pg_session)
            if d.revoked_at is None
        ]
        assert len(alive) == 2

    def test_marking_the_operator_row(self, admin, pg_session, user_id):
        sign_in(admin)

        admin.post(f"/admin/users/{user_id}/operator", data={"operator": "1"})

        assert users.find_admin(pg_session).id == user_id

    def test_revoking_someone_elses_device_takes_two_clicks(
        self, admin, pg_session, user_id, secrets
    ):
        """本人自己也能吊。这一条是给「他打不开控制台」那种情况准备的 ——
        最典型的就是手机本身丢了,而那部手机正是他打开控制台的地方。"""
        from lifein.repos import credentials

        sign_in(admin)

        first = admin.post(
            f"/admin/users/{user_id}/devices/revoke", data={"device_id": "pixel-7a"}
        )
        assert "吊销" in first.text
        alive = [
            d
            for d in credentials.list_device_credentials(user_id, pg_session)
            if d.revoked_at is None
        ]
        assert len(alive) == 2

        admin.post(
            f"/admin/users/{user_id}/devices/revoke",
            data={"device_id": "pixel-7a", "confirm": "yes"},
        )
        alive = [
            d
            for d in credentials.list_device_credentials(user_id, pg_session)
            if d.revoked_at is None
        ]
        assert alive == []


class TestSwitchingLayers:
    def test_it_goes_through_the_door_the_user_layer_already_has(
        self, admin, pg_session, user_id
    ):
        """**没有为这个按钮发明第二条认证路径。**

        它签一张一次性链接,跳过去,那边照常把 token 换成 cookie ——
        发明一条新的就多一处会出错的地方,而这一条已经被测试盯了很久。
        """
        sign_in(admin)
        users.set_admin(user_id, pg_session, is_admin=True)

        response = admin.post("/admin/switch", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"].startswith("/console?t=")

    def test_it_switches_to_the_operators_own_account(self, admin, pg_session, user_id):
        """**切到运营者自己的账号,不是别人的。**"""
        other = "77777777-7777-7777-7777-777777777777"
        pg_session.execute(
            text(
                "INSERT INTO users (id, display_name, wecom_userid)"
                " VALUES (:i, '别人', 'other-switch')"
            ),
            {"i": other},
        )
        users.set_admin(user_id, pg_session, is_admin=True)
        sign_in(admin)

        location = admin.post("/admin/switch", follow_redirects=False).headers["location"]
        landed = admin.get(location, follow_redirects=True).text

        assert "测试用户" in landed
        assert "别人" not in landed

    def test_without_a_marked_row_it_explains_instead_of_guessing(
        self, admin, pg_session, user_id
    ):
        """**不猜一个账号。** 猜错了会安静地把运营者带进别人的数据 ——
        页面上写着「你的设备」「你的数据」,只是那个「你」是别人。"""
        sign_in(admin)

        page = admin.post("/admin/switch").text

        assert "还没有哪一行标成运营者" in page

    def test_it_is_not_a_link(self, admin, pg_session, user_id):
        """**签发是一个会改变库的动作。** GET 会被浏览器预取,
        而预取一次就多一张有效的链接躺在那儿。"""
        sign_in(admin)

        assert admin.get("/admin/switch").status_code == 405


class TestTheShell:
    def test_there_are_no_external_references(self, admin, pg_session, user_id):
        sign_in(admin)

        for path in ("/admin", "/admin/users", "/admin/ops", f"/admin/users/{user_id}"):
            page = admin.get(path).text
            assert "<img" not in page, path
            assert "src=" not in page, path
            assert 'href="http' not in page, path
            assert "<script" not in page, path

    def test_the_ops_page_names_the_three_rotting_things(self, admin, pg_session, user_id):
        """三样都是**不看就会静静烂掉**的那一类:一个永远失败的窗口不会报警,
        一条卡住的审批不会自己动,而轮换到一半的凭据在旧密钥被删掉之前
        一直都能用 —— 然后某天全都解不开。"""
        sign_in(admin)

        page = admin.get("/admin/ops").text

        assert "失败的窗口" in page
        assert "卡住的审批" in page
        assert "旧密钥残留" in page

    def test_a_missing_user_says_so(self, admin, pg_session, user_id):
        sign_in(admin)

        page = admin.get("/admin/users/00000000-0000-0000-0000-000000000000").text

        assert "没有这个用户" in page
