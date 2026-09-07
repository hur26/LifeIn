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

NO_USER_ID_REQUIRED = frozenset({"find_by_wecom_userid", "create_user"})
"""不以 user_id 开头的函数白名单。

只有两类可以进来:**把外部标识换成内部 id 的**,以及**创建用户的**。
其他任何函数都必须带 user_id —— 铁律 1 在 repos/__init__.py。
"""

_SELECT_BY_WECOM = text("""
    SELECT id, display_name, wecom_userid, tz, disabled_at
      FROM users
     WHERE wecom_userid = :wecom_userid
""")

_SELECT_BY_ID = text("""
    SELECT id, display_name, wecom_userid, tz, disabled_at
      FROM users
     WHERE id = :user_id
""")

_INSERT = text("""
    INSERT INTO users (display_name, wecom_userid, tz)
    VALUES (:display_name, :wecom_userid, :tz)
    RETURNING id
""")


@dataclass(frozen=True)
class User:
    id: str
    display_name: str
    wecom_userid: str
    tz: str
    disabled_at: datetime | None

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


def create_user(
    session: Session, *, display_name: str, wecom_userid: str, tz: str = "Asia/Shanghai"
) -> str:
    """建用户。P0 只会调一次(部署时),P4 才会有第二次。"""
    return str(
        session.execute(
            _INSERT,
            {"display_name": display_name, "wecom_userid": wecom_userid, "tz": tz},
        ).scalar_one()
    )
