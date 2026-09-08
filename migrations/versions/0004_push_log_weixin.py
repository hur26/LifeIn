"""push_log.channel 放行 weixin。

Revision ID: 0004
Revises: 0003

0001 里这条 CHECK 写的是 ('wecom','email') —— 那是 iLink 还不存在时的世界。
加了微信通道之后,第一次真实推送就撞上了:**消息发出去了,记录写不进去**,
事务回滚,任务被标成 failed,而下次重跑同一个窗口会再发一遍。

约束本身没错,它确实拦住了一个未知的通道名。错的是加通道时没跟着改它 ——
这类"数据库比代码少认识一个枚举值"的问题,只有真跑一次才会暴露。
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

CONSTRAINT = "push_log_channel_check"


def upgrade() -> None:
    op.execute(f"ALTER TABLE push_log DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.execute(
        f"ALTER TABLE push_log ADD CONSTRAINT {CONSTRAINT} "
        "CHECK (channel IN ('weixin','wecom','email'))"
    )


def downgrade() -> None:
    # 回滚前得先把 weixin 的记录改掉,否则加不上旧约束。
    # 不删记录 —— push_log 是频率闸门的计数来源,删了会让闸门算错
    op.execute("UPDATE push_log SET channel = 'wecom' WHERE channel = 'weixin'")
    op.execute(f"ALTER TABLE push_log DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.execute(
        f"ALTER TABLE push_log ADD CONSTRAINT {CONSTRAINT} "
        "CHECK (channel IN ('wecom','email'))"
    )
