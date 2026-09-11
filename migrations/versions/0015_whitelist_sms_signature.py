"""`collector_whitelist` 放行 `sms_signature`,并把号段规则停用。

ADR-034:银行短信改按**短信签名**(正文开头 `【…】` 里的机构名)匹配,
不按发件人。理由是三条真实短信同时证伪了两个假设 ——

- 通知标题有时是显示名(`招商银行`)有时是网关号码(`10693495555`)
- 真实号码走 1069 的 SP 网关,`95555` 在里面是**子串不是前缀**,
  所以那 14 条号段预设**就算拿得到号码也一条都命中不了**

**`sms_sender` 不从 CHECK 里删。** 06 §6.9 那条"没有删除"管的就是这张表:
停用即不放行,而留着那一行能回答"曾经放行过谁" —— 删掉约束值等于让历史行
非法,那比留着一个不推荐的值糟。

**但库里现存的号段规则要停用。** 它们永远不会命中,而留着启用状态会让
`list-sources` 和状态页继续显示"这家银行放行着" —— 那正是这次事故里最伤人的
一点:看起来配好了,一条都收不到。停用之后那一行还在,历史照样查得到。

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

NAME = "collector_whitelist_match_type_check"
OLD = "CHECK (match_type IN ('sms_sender','package_name'))"
NEW = "CHECK (match_type IN ('sms_sender','package_name','sms_signature'))"


def upgrade() -> None:
    op.execute(f"ALTER TABLE collector_whitelist DROP CONSTRAINT IF EXISTS {NAME}")
    op.execute(f"ALTER TABLE collector_whitelist ADD CONSTRAINT {NAME} {NEW}")
    # 停用,不删除。**只动号段那一类** —— 用户自己按包名配的不受影响
    op.execute(
        "UPDATE collector_whitelist SET enabled = false "
        "WHERE match_type = 'sms_sender' AND enabled = true"
    )


def downgrade() -> None:
    # **降级会删掉签名规则。** 没有别的办法:它们在旧约束下非法,而把它们改成
    # 某个 sms_sender 号段是猜的(签名是机构名,反推不出号码)。
    # 停用也不行 —— CHECK 不管一行启没启用。
    #
    # 那些号段规则**不会被重新启用**:这里不知道它们在升级之前是开是关,
    # 而猜错的方向是"悄悄开始采集",比"少采一点"糟。
    op.execute("DELETE FROM collector_whitelist WHERE match_type = 'sms_signature'")
    op.execute(f"ALTER TABLE collector_whitelist DROP CONSTRAINT IF EXISTS {NAME}")
    op.execute(f"ALTER TABLE collector_whitelist ADD CONSTRAINT {NAME} {OLD}")
