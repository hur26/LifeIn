"""console_links:控制台的一次性链接(P4 第 9 片)。

Revision ID: 0011
Revises: 0010

**浏览器打开一个链接时带不了 `Authorization` 头** —— App 里那套 Bearer token
在 Web 上直接用不了。这张表是那个问题的答案:App 里点一下换一个短命 token,
放进 URL 跳过去。

选它而不是"用户名密码 + 会话 cookie",是因为**多一个认证面就多一处会被攻破的
地方**,而这个系统的凭据面已经够多了(R11)。

存哈希不存明文,和 `enrollment_codes` 一样:库被拖走时,里面的东西不该直接可用。
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE console_links (
    id         BIGSERIAL PRIMARY KEY,
    user_id    UUID NOT NULL,
    token_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (token_hash)
)
"""


def upgrade() -> None:
    op.execute(TABLE)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS console_links")
