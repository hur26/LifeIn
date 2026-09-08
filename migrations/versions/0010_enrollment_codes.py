"""enrollment_codes:一次性换取码(P4 第 1 片)。

Revision ID: 0010
Revises: 0009

在这之前,配码二维码里是**明文的两把密钥**。自己扫没问题;而 03 的 P4 说
朋友要用它配码,那张图会走微信发过去 —— 等于把密钥发在聊天里,
而微信的聊天记录会漫游、会备份、会被截图。

这张表存的是**码的哈希,不是码本身**。理由和密码一样:库被拖走时,
里面的东西不该直接可用。码只在生成的那一刻显示一次。

`claimed_at` 而不是删行:**换过的码要留下痕迹**。"这台设备是什么时候、
用哪个码配上的"是排查"我的号被别人配走了吗"唯一的线索,
而删掉那一行之后这个问题就没法回答了。
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE enrollment_codes (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL,
    code_hash   TEXT NOT NULL,          -- sha256(码),码本身只显示一次
    purpose     TEXT NOT NULL DEFAULT 'all'
                  CHECK (purpose IN ('all', 'collect', 'query')),
    base_url    TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    claimed_at  TIMESTAMPTZ,            -- 换过了。不删行:留痕迹
    claimed_by  TEXT,                   -- App 自己生成并上报的 device_id
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (code_hash)
)
"""

# 查的时候只按 code_hash 找,而那是唯一键 —— 不需要额外索引。
# 清理过期的那条查询按 expires_at,一个月几十行,全表扫也无所谓
INDEX = "CREATE INDEX ON enrollment_codes (user_id, created_at DESC)"


def upgrade() -> None:
    op.execute(TABLE)
    op.execute(INDEX)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS enrollment_codes")
