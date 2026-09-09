"""`approvals` 加 `executing`:代发消息原来会真的发两条。

Revision ID: 0013
Revises: 0012

执行 job 原来的顺序是**先执行、再改状态**,写在那个模块开头的理由是
"改完状态到执行完成之间那一瞬如果进程挂了,那条审批会停在 executed 而事情
根本没做 —— 宁可有极小的概率重发一条"。

那个取舍算错了两件事:

1. **概率不小。** `list_ready` 取的是 status='approved' 的行,两个执行者会
   同时取到同一条、同时执行、同时发出去。"极小的概率"描述的是崩溃窗口,
   而这里的窗口是**整个执行时长** —— 代发一条消息要等对面几秒钟
2. **代价的方向反了。** 03 的 P3 退出条件写着"出现任何一次重复执行 →
   停止 L3 上线",而"漏做"不在退出条件里

所以加一个中间状态,认领和执行分开:

    approved --认领--> executing --+--> executed
                                   +--> failed

`started_at` 记认领的时刻。**卡住的 executing 靠它才判得出来** ——
没有它就只能看 approved_at,而那是"人点同意"的时间,和执行差着一整个调度周期。

**已有的行不用动。** executing 是新状态,库里现在一条都没有;
approved 的那些下一次运行会正常被认领。
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

OLD = (
    "CHECK (status IN ('pending','approved','rejected',"
    "'executed','expired','failed'))"
)
NEW = (
    "CHECK (status IN ('pending','approved','executing','rejected',"
    "'executed','expired','failed'))"
)
NAME = "approvals_status_check"


def upgrade() -> None:
    op.execute(f"ALTER TABLE approvals DROP CONSTRAINT IF EXISTS {NAME}")
    op.execute(f"ALTER TABLE approvals ADD CONSTRAINT {NAME} {NEW}")
    op.execute("ALTER TABLE approvals ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ")


def downgrade() -> None:
    # 回滚前把卡住的那些放回 approved。**不能直接删约束里的 executing** ——
    # 那样 ALTER 会因为存在违反约束的行而失败,而报的错只说"约束建不上"
    op.execute("UPDATE approvals SET status = 'approved' WHERE status = 'executing'")
    op.execute(f"ALTER TABLE approvals DROP CONSTRAINT IF EXISTS {NAME}")
    op.execute(f"ALTER TABLE approvals ADD CONSTRAINT {NAME} {OLD}")
    op.execute("ALTER TABLE approvals DROP COLUMN IF EXISTS started_at")
