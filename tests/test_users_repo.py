"""`users` 上这一版新加的三样:运营者标记、连停用一起枚举、停用与恢复。

需要真实 PostgreSQL —— 这一组顺带把迁移 0014 跑一遍
(夹具每次从 base 重建到 head)。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from lifein.repos import users
from tests.conftest import NOW

pytestmark = pytest.mark.integration


def another(session, name: str = "别人", wecom: str = "other") -> str:
    return users.create_user(session, display_name=name, wecom_userid=wecom)


class TestTheAdminFlag:
    def test_nobody_is_admin_by_default(self, pg_session, user_id):
        """**已经躺在库里的那些用户是朋友,不是运营者**(迁移 0014 的 DEFAULT false)。"""
        assert users.get_user(user_id, pg_session).is_admin is False
        assert users.find_admin(pg_session) is None

    def test_it_finds_the_marked_one(self, pg_session, user_id):
        other = another(pg_session)
        users.set_admin(other, pg_session, is_admin=True)

        assert users.find_admin(pg_session).id == other

    def test_creating_one_marked(self, pg_session):
        created = users.create_user(
            pg_session, display_name="白杨", wecom_userid="baiyang", is_admin=True
        )

        assert users.find_admin(pg_session).id == created

    def test_unmarking_leaves_the_user_alone(self, pg_session, user_id):
        """**这一列不是权限位,取消标记不该动别的任何东西。**"""
        users.set_admin(user_id, pg_session, is_admin=True)
        users.set_admin(user_id, pg_session, is_admin=False)

        user = users.get_user(user_id, pg_session)
        assert user is not None and user.is_admin is False
        assert users.find_admin(pg_session) is None

    def test_two_marked_rows_do_not_blow_up(self, pg_session, user_id):
        """**标了两行时取最早的那个,不报错。**

        控制台上少一个按钮比整个页面 500 好,而"标了两行"在用户列表上
        一眼看得见。
        """
        users.set_admin(user_id, pg_session, is_admin=True)
        users.set_admin(another(pg_session), pg_session, is_admin=True)

        assert users.find_admin(pg_session).id == user_id


class TestListing:
    def test_all_includes_the_disabled_ones(self, pg_session, user_id):
        """**看不见就恢复不了** —— 运营台的用户列表要有停用的那些。"""
        other = another(pg_session)
        users.set_disabled(other, pg_session, disabled_at=NOW)

        assert other in users.list_all_users(pg_session)
        assert other not in users.list_active_users(pg_session)

    def test_it_returns_ids_only(self, pg_session, user_id):
        """**"枚举"这个例外不会顺手变成"批量读数据"**(铁律 1)。"""
        listed = users.list_all_users(pg_session)

        assert listed and all(isinstance(one, str) for one in listed)


class TestDisabling:
    def test_disabling_and_restoring(self, pg_session, user_id):
        users.set_disabled(user_id, pg_session, disabled_at=NOW)
        assert users.get_user(user_id, pg_session).active is False

        users.set_disabled(user_id, pg_session, disabled_at=None)
        assert users.get_user(user_id, pg_session).active is True

    def test_disabling_does_not_touch_the_data(self, pg_session, user_id):
        """**停用不删除:历史事件仍要能追溯到人**(06 §2.10)。"""
        users.set_disabled(user_id, pg_session, disabled_at=NOW)

        row = pg_session.execute(
            text("SELECT display_name FROM users WHERE id = :u"), {"u": user_id}
        ).scalar_one()
        assert row

    def test_disabling_does_not_revoke_devices(self, pg_session, user_id, secrets):
        """**两件事分开是有意的**:"先停下来别再花钱扫他的邮箱"和"这台手机丢了"
        不是同一件事。"""
        from lifein.repos import credentials

        users.set_disabled(user_id, pg_session, disabled_at=NOW + timedelta(seconds=1))

        alive = [
            d for d in credentials.list_device_credentials(user_id, pg_session) if not d.revoked_at
        ]
        assert alive, "停用一个用户不该顺手吊销他的设备"
