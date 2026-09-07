"""job_runs:定时任务的执行窗口记录。

Revision ID: 0002
Revises: 0001

ADR-016 说补偿"靠数据库记录上次执行窗口实现,不依赖调度器自身的持久化"。
这张表就是那个记录 —— 写 0001 时漏了,因为它在 06 里也是后补的。

单独一次迁移而不是改 0001:06 §3 要求"一次迁移一个语义变更"。
0001 已经被跑过,回头改它意味着已部署的库和迁移脚本对不上。
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE job_runs (
    id           BIGSERIAL PRIMARY KEY,
    user_id      UUID NOT NULL,
    job_name     TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end   TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('running','succeeded','failed')),
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    error        TEXT,
    stats        JSONB NOT NULL DEFAULT '{}',
    UNIQUE (user_id, job_name, window_start)
)
"""

INDEX = "CREATE INDEX ix_job_runs_user_job_window ON job_runs (user_id, job_name, window_end DESC)"


def upgrade() -> None:
    op.execute(TABLE)
    op.execute(INDEX)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS job_runs")
