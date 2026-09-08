"""`rule_state` 的读写 —— 每条主动规则的开关(06 §2.12)。

**没有记录就是 `shadow`。** 这条默认值是整个模块的要害:新加一条规则时不写
这张表,它只会记录不会推送 —— 忘记配置的后果是安静,不是打扰。反过来
(默认 active)的话,某天有人加条规则忘了说,用户第二天就被吵到,
而 R4 说误报两次就足够让人永久关掉通知。
"""

from __future__ import annotations

import logging
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


class RuleMode(StrEnum):
    SHADOW = "shadow"
    """只记录不推送。**新规则的默认状态**,观察期结束才转 active。"""

    ACTIVE = "active"
    OFF = "off"
    """用户主动关掉的。和"还在观察期"的 shadow 分开:前者不该再被自动转成
    active,后者迟早要转。"""


_SELECT = text("SELECT mode FROM rule_state WHERE user_id = :user_id AND rule_id = :rule_id")

_UPSERT = text("""
    INSERT INTO rule_state (user_id, rule_id, mode)
    VALUES (:user_id, :rule_id, :mode)
    ON CONFLICT (user_id, rule_id)
    DO UPDATE SET mode = EXCLUDED.mode, updated_at = now()
""")

_LIST = text("SELECT rule_id, mode FROM rule_state WHERE user_id = :user_id ORDER BY rule_id")


def get_mode(user_id: str, session: Session, *, rule_id: str) -> RuleMode:
    """取一条规则的模式。**没有记录就是 shadow。**"""
    row = session.execute(_SELECT, {"user_id": user_id, "rule_id": rule_id}).first()
    return RuleMode(row.mode) if row else RuleMode.SHADOW


def set_mode(user_id: str, session: Session, *, rule_id: str, mode: RuleMode) -> None:
    """改一条规则的模式。转 active 之前该先看过影子期的数据。"""
    session.execute(
        _UPSERT, {"user_id": user_id, "rule_id": rule_id, "mode": mode.value}
    )


def all_modes(user_id: str, session: Session) -> dict[str, RuleMode]:
    """已经显式配过的那些。没出现在结果里的规则都处在 shadow。"""
    rows = session.execute(_LIST, {"user_id": user_id}).all()
    return {row.rule_id: RuleMode(row.mode) for row in rows}
