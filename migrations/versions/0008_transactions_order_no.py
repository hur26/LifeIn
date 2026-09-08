"""transactions.order_no:对账单回填的订单号。

Revision ID: 0008
Revises: 0007

ADR-012 的两阶段入账写着月度账单到达后"回填真实商户名、订单号",
但 0001 建表时没有这一列 —— 商户名有地方放(`merchant_raw`),订单号没有。

订单号是**跟支付平台对话时唯一有用的东西**:一笔有争议的消费,
支付宝客服要的是订单号,不是金额和时间。所以它值得单独一列,
而不是塞进某个 JSON 里。

**只有对账单那一道给得出它。** 实时通知里没有订单号,
所以实时入账的那些行这一列永远是空的,这是正常状态,不是缺数据。
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE transactions ADD COLUMN order_no TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE transactions DROP COLUMN IF EXISTS order_no")
