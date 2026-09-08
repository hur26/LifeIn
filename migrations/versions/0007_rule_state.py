"""rule_state:主动规则的开关。

Revision ID: 0007
Revises: 0006

架构 §4 的"每条规则有 mode"和产品定义 §5 的"每条主动推送都要能一键关闭"
落在这张表上。结构与 06 §2.12 一致。

**没有记录就是 shadow**,所以这张表建出来是空的,而那正是要的状态:
新规则一律先只记录不推送,忘记配置的后果是安静而不是打扰(R4)。
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE rule_state (
    user_id    UUID NOT NULL,
    rule_id    TEXT NOT NULL,
    mode       TEXT NOT NULL DEFAULT 'shadow',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, rule_id),
    CONSTRAINT rule_state_mode_known CHECK (mode IN ('shadow', 'active', 'off'))
)
"""


def upgrade() -> None:
    op.execute(TABLE)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rule_state")
