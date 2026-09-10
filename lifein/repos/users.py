"""`users` 的读写。

这是唯一一个**必须**有函数不以 `user_id` 开头的模块 —— 身份解析的那一步
手上还没有 `user_id`,它正是要算出这个值。企微回调只带得来一个
`wecom_userid`,总得有地方把它换成内部 id。

例外用 `NO_USER_ID_REQUIRED` 显式列出来,而不是靠"这个模块特殊"这种默契。
加一个新的例外意味着改这个集合,而改它是个看得见的动作
(`tests/test_repo_contract.py` 读的就是它)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

NO_USER_ID_REQUIRED = frozenset(
    {"find_by_wecom_userid", "find_admin", "create_user", "list_active_users", "list_all_users"}
)
"""不以 user_id 开头的函数白名单。

只有三类可以进来:**把外部标识换成内部 id 的**、**创建用户的**、
**枚举用户的**(定时任务要知道该给谁跑)。
其他任何函数都必须带 user_id —— 铁律 1 在 repos/__init__.py。

`find_admin` 是第一类的第二个成员,和 `find_by_wecom_userid` 同一个形状:
运营台的会话 cookie 只说明"你是运营者",而这个函数正是把那句话换成一个
`user_id`(ADR-029)。`list_all_users` 是第三类的第二个成员 ——
**它只返回 id**,和 `list_active_users` 一样,拿到之后照样要走 `get_user`。
"""

_COLUMNS = "id, display_name, wecom_userid, tz, is_admin, disabled_at, created_at"

_SELECT_BY_WECOM = text(f"SELECT {_COLUMNS} FROM users WHERE wecom_userid = :wecom_userid")

_SELECT_BY_ID = text(f"SELECT {_COLUMNS} FROM users WHERE id = :user_id")

_SELECT_ADMIN = text(f"SELECT {_COLUMNS} FROM users WHERE is_admin ORDER BY created_at LIMIT 1")

_INSERT = text("""
    INSERT INTO users (display_name, wecom_userid, tz, is_admin)
    VALUES (:display_name, :wecom_userid, :tz, :is_admin)
    RETURNING id
""")


@dataclass(frozen=True)
class User:
    id: str
    display_name: str
    wecom_userid: str
    tz: str
    disabled_at: datetime | None
    is_admin: bool = False
    created_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.disabled_at is None


def _row_to_user(row) -> User:
    return User(
        id=str(row.id),
        display_name=row.display_name,
        wecom_userid=row.wecom_userid,
        tz=row.tz,
        disabled_at=row.disabled_at,
        is_admin=bool(row.is_admin),
        created_at=row.created_at,
    )


def find_by_wecom_userid(session: Session, *, wecom_userid: str) -> User | None:
    """把企微 userid 换成内部用户。找不到返回 None。

    **找不到不是错误。** 企业里任何人都能给自建应用发消息,而这个系统只服务
    库里有记录的那些 —— 陌生人发来的消息直接丢掉,连回复都不给
    (回复等于告诉对方"这个地址是活的")。
    """
    row = session.execute(_SELECT_BY_WECOM, {"wecom_userid": wecom_userid}).first()
    return _row_to_user(row) if row else None


def get_user(user_id: str, session: Session) -> User | None:
    row = session.execute(_SELECT_BY_ID, {"user_id": user_id}).first()
    return _row_to_user(row) if row else None


def find_admin(session: Session) -> User | None:
    """运营者自己的那一行。没有就返回 None。

    **没有不是错误。** 运营层的口令在环境变量里,和这一列没关系 ——
    一行都没标的时候运营台照样能登,只是那个"切回普通用户版"的按钮
    没有目标,于是它不显示(ADR-029)。

    标了不止一行时取最早的那个,并且**不报错**:控制台上少一个按钮
    比整个页面 500 好,而"标了两行"这件事在用户列表上一眼看得见。
    """
    row = session.execute(_SELECT_ADMIN).first()
    return _row_to_user(row) if row else None


def create_user(
    session: Session,
    *,
    display_name: str,
    wecom_userid: str,
    tz: str = "Asia/Shanghai",
    is_admin: bool = False,
) -> str:
    """建用户。P0 只会调一次(部署时),P4 才会有第二次。"""
    return str(
        session.execute(
            _INSERT,
            {
                "display_name": display_name,
                "wecom_userid": wecom_userid,
                "tz": tz,
                "is_admin": is_admin,
            },
        ).scalar_one()
    )


def set_admin(user_id: str, session: Session, *, is_admin: bool) -> bool:
    """标记(或取消标记)运营者本人。返回有没有改到行。"""
    return (
        session.execute(
            text("UPDATE users SET is_admin = :flag WHERE id = :user_id"),
            {"flag": is_admin, "user_id": user_id},
        ).rowcount
        > 0
    )


def set_disabled(user_id: str, session: Session, *, disabled_at: datetime | None) -> bool:
    """停用或恢复一个用户。**停用不删数据** —— 历史事件仍要能追溯到人(06 §2.10)。

    停用之后 `list_active_users` 不再返回他,于是定时任务不再给他跑;
    已经签发的设备凭据**不受影响**,要单独吊销。两件事分开是有意的:
    "先停下来别再花钱扫他的邮箱"和"这台手机丢了"不是同一件事。
    """
    return (
        session.execute(
            text("UPDATE users SET disabled_at = :at WHERE id = :user_id"),
            {"at": disabled_at, "user_id": user_id},
        ).rowcount
        > 0
    )


def list_active_users(session: Session) -> list[str]:
    """列出所有未停用的用户 id。定时任务要知道该给谁跑。

    只返回 id,不返回别的字段 —— 调用方拿到 id 之后走正常的 get_user,
    那条路径是带 user_id 的。这样"枚举"这个例外不会顺手变成"批量读数据"。
    """
    rows = session.execute(
        text("SELECT id FROM users WHERE disabled_at IS NULL ORDER BY created_at")
    ).all()
    return [str(r.id) for r in rows]


def list_all_users(session: Session) -> list[str]:
    """列出所有用户 id,**包括停用的**。运营台的用户列表要能看见停用的那些 ——
    看不见就恢复不了。

    和 `list_active_users` 一样只返回 id:调用方拿到 id 之后走 `get_user`,
    那条路径是带 user_id 的。**"枚举"这个例外不会顺手变成"批量读数据"。**
    """
    rows = session.execute(text("SELECT id FROM users ORDER BY created_at")).all()
    return [str(r.id) for r in rows]
