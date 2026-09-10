"""`users.is_admin`:运营层控制台上那个"切回普通用户版"要知道切到哪。

Revision ID: 0014
Revises: 0013

控制台从这一版起分两层([ADR-029](../../docs/04-tech-decisions.md)):用户层不登录,
运营层要口令。而**运营者自己也用这套东西** —— 他在运营台上看完全局,
下一步多半是回自己的账号看一眼今天有什么。

那个按钮要一个 `user_id`。不加这一列的话它只能从环境变量里抄一个 uuid 进来,
而**一个抄错了的 uuid 会安静地把运营者带进别人的数据** —— 不报错,
页面上显示的是"你的设备""你的数据",只是那个"你"是别人。

**这一列不是权限位。** 运营层的口令是环境变量里的一个 scrypt 派生值
(07 §2.8),不在这张表里 —— 把这一列改成 `true` 不会让谁登得进运营层。
反过来也一样:这一列全是 `false` 时运营层照样能登,只是那个切换按钮没有目标。

**DEFAULT false**:已经躺在库里的那些用户是朋友,不是运营者。
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_admin")
