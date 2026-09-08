"""l2_needs_rollback 只管放行了的调用。

Revision ID: 0006
Revises: 0005

原来的约束是 `level <> 'L2' OR rollback_info IS NOT NULL` —— 它把**所有** L2
审计都要求带回滚信息,包括那些**根本没执行**的:入参不合法被拒、不在 agent
白名单里被拒、工具执行中抛异常。那三种情况本来就没有回滚信息可写。

后果不是报错,是**审计悄悄少了一整类记录**:写入被库拒 → `record_tool_call`
按设计吞掉异常(审计失败不该让业务失败)→ 谁都不知道有人试过一次 L2 调用
并且被拦下了。网关第 5 道写的是"无论结果如何都记审计,**包括被拒的**",
而对 L2 那句话此前是空的。

新约束只在 `result_status = 'allowed'` 时要求回滚信息 —— 也就是**真的执行了**
的那些。安全性没有松:执行过却没有回滚信息的 L2 调用,照样写不进审计表,
也就等于执行不了。

`error` 一档要特别说一句:它意味着**副作用可能已经发生却拿不到回滚信息**,
这正是最需要留下痕迹的一种,以前反而是唯一一种记不下来的。

这个错只有连真库才发现得了:单元测试用的是内存 sink,碰不到这条 CHECK。
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

CONSTRAINT = "l2_needs_rollback"

NEW = f"""
ALTER TABLE tool_calls ADD CONSTRAINT {CONSTRAINT}
    CHECK (level <> 'L2' OR result_status <> 'allowed' OR rollback_info IS NOT NULL)
"""

OLD = f"""
ALTER TABLE tool_calls ADD CONSTRAINT {CONSTRAINT}
    CHECK (level <> 'L2' OR rollback_info IS NOT NULL)
"""


def upgrade() -> None:
    op.execute(f"ALTER TABLE tool_calls DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.execute(NEW)


def downgrade() -> None:
    # 回到旧约束前先清掉那些新约束允许、旧约束不允许的行,
    # 否则 ADD CONSTRAINT 会因为已有数据不满足而失败
    op.execute("DELETE FROM tool_calls WHERE level = 'L2' AND rollback_info IS NULL")
    op.execute(f"ALTER TABLE tool_calls DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.execute(OLD)
