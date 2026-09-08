"""channel_state:入站通道的少量状态。

Revision ID: 0003
Revises: 0002

iLink 长轮询需要记住游标(不记就会在重启后重放旧消息 —— 重复回答、重复花钱),
以及每个对话方最近一次的 context_token(协议要求回复时原样带回)。

做成通用 KV 而不是给 iLink 开专表:下一个入站通道也会有类似的东西,
而这类状态丢了不会出事(重新同步即可),不值得各自建表。
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE channel_state (
    user_id    UUID NOT NULL,
    channel    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, channel, key)
)
"""


def upgrade() -> None:
    op.execute(TABLE)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS channel_state")
