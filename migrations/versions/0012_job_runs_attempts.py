"""`job_runs.attempts`:失败的窗口要能被重跑。

Revision ID: 0012
Revises: 0011

`claim_window` 原来是 `ON CONFLICT DO NOTHING`,`windows_to_run` 只从
**最后一个成功窗口**往后切。两条规则单看都对,合起来"失败"和"成功"
对下一次运行没有任何区别:失败的那一天被切出来,撞上已存在的行,跳过;
后面的窗口成功之后成功点推进,那一天从此再也不会被切出来。

丢的是那一整天的事件 —— 记账、日程、记忆一起。**而它不报错**:
日志里只有一行早已被滚掉的 WARNING,账本上只是"这天没花钱"。

加一列计数,认领改成有条件的 `DO UPDATE`(判断和写入同一条语句,
并发下不会两个进程同时认领)。三次之后不再重跑:无限重试比放弃更糟 ——
一个永远失败的窗口会把后面每一天的补偿额度都吃掉。

**DEFAULT 0 而不是 1**:已经躺在库里的那些 failed 行是这次要救的对象,
给它们 0 意味着迁移之后它们还剩满额的三次机会。
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE job_runs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE job_runs DROP COLUMN IF EXISTS attempts")
