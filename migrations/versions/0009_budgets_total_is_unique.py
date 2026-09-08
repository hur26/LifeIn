"""总预算也要唯一:`UNIQUE NULLS NOT DISTINCT`。

Revision ID: 0009
Revises: 0008

0001 建表时写的是 `UNIQUE (user_id, category, period)`,而**总预算的
`category` 是 NULL** —— Postgres 默认把 NULL 之间看成互不相同,
于是那条约束对总预算根本不起作用:`set-budget --amount 5000` 跑两次
会留下两条总预算,而 `ON CONFLICT` 也永远撞不上。

症状不是报错,是**两条预算各发各的预警**:同一个月你会收到两次"总预算超了",
金额还不一样(其中一条是旧额度)。改额度这个动作在用户眼里失败了,
但没有任何地方告诉他。

Postgres 15 起可以写 `NULLS NOT DISTINCT`,把 NULL 之间也当成相等。
这台是 16,所以直接用它,不用"给总预算塞一个哨兵字符串"那种绕法 ——
哨兵会漏进每一个查询,而漏掉一处就是一个错的数字。

**不能只加索引不删旧约束**:旧的那条不挡总预算,留着只是让人以为挡住了。
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# 0001 里那条约束是 UNIQUE(...) 自动建的,名字按 Postgres 的规则拼出来
OLD = "budgets_user_id_category_period_key"
NEW = "budgets_user_category_period_uniq"


def upgrade() -> None:
    # 先去重再加约束:已经存在的重复总预算会让新约束建不上,
    # 而那时报的错("could not create unique index")完全不解释发生了什么。
    # 留下 id 最大的那条 —— 它是最后设的,也就是用户以为生效的那个额度
    op.execute("""
        DELETE FROM budgets a
         USING budgets b
         WHERE a.user_id = b.user_id
           AND a.period = b.period
           AND a.category IS NULL AND b.category IS NULL
           AND a.id < b.id
    """)
    op.execute(f"ALTER TABLE budgets DROP CONSTRAINT IF EXISTS {OLD}")
    op.execute(
        f"ALTER TABLE budgets ADD CONSTRAINT {NEW} "
        "UNIQUE NULLS NOT DISTINCT (user_id, category, period)"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE budgets DROP CONSTRAINT IF EXISTS {NEW}")
    op.execute(
        f"ALTER TABLE budgets ADD CONSTRAINT {OLD} UNIQUE (user_id, category, period)"
    )
