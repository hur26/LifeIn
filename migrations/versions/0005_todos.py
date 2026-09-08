"""todos:待办与待写入的日程。

Revision ID: 0005
Revises: 0004

ADR-020 把待办和日程的落地点定在自己这边:App 是它的界面,不是另一个数据源。
表结构与 06 §2.11 一致 —— 那份文档是真相,这里是它的可执行版本。

两条 CHECK 都是拦"看起来正常、实际不可能成功"的行:

- `todos_schedule_needs_time`:没有时间的东西写不进系统日历。放进来只会变成
  设备端一次必然失败的写入,而失败发生在手机上 —— 服务端只看得到"一直没同步"
- `todos_agent_needs_provenance`:**和 facts 那条是同一条铁律 5**。
  用户自己加的待办不需要出处,系统替他加的必须说得出为什么。用 `cardinality`
  而不是 `array_length` 的原因见 06 §2.3(空数组的 array_length 返回 NULL,
  CHECK 遇上 NULL 判定通过,约束形同虚设)

部分索引 `WHERE synced_at IS NULL` 服务的是同步队列:App 每次只问"还有什么
没落地",那是这张表上最频繁的查询,而它永远只关心一小部分行。
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE todos (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL,
    kind             TEXT NOT NULL,
    title            TEXT NOT NULL,
    notes            TEXT,
    starts_at        TIMESTAMPTZ,
    ends_at          TIMESTAMPTZ,
    status           TEXT NOT NULL DEFAULT 'open',
    source           TEXT NOT NULL,
    provenance       BIGINT[] NOT NULL DEFAULT '{}',
    device_ref       TEXT,
    synced_at        TIMESTAMPTZ,
    created_by_agent TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT todos_kind_known CHECK (kind IN ('todo', 'schedule')),
    CONSTRAINT todos_status_known CHECK (status IN ('open', 'done', 'cancelled')),
    CONSTRAINT todos_source_known CHECK (source IN ('agent', 'user')),
    CONSTRAINT todos_schedule_needs_time
        CHECK (kind <> 'schedule' OR starts_at IS NOT NULL),
    CONSTRAINT todos_agent_needs_provenance
        CHECK (source <> 'agent' OR cardinality(provenance) >= 1)
)
"""

INDEXES = [
    "CREATE INDEX ix_todos_user_status_time ON todos (user_id, status, starts_at)",
    "CREATE INDEX ix_todos_user_unsynced ON todos (user_id) "
    "WHERE synced_at IS NULL AND kind = 'schedule'",
]


def upgrade() -> None:
    op.execute(TABLE)
    for statement in INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS todos")
