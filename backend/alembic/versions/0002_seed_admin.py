"""种子数据：初始 admin 用户。

密码来自 settings.admin_initial_password（.env 注入，服务器必须覆盖为强密码），
哈希在迁移执行时计算，不落明文。

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-25
"""

import sqlalchemy as sa
from alembic import op

from core.config import settings

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    import bcrypt

    password_hash = bcrypt.hashpw(
        settings.admin_initial_password.encode(), bcrypt.gensalt()
    ).decode()
    users = sa.table(
        "users",
        sa.column("username", sa.Text),
        sa.column("password_hash", sa.Text),
    )
    op.bulk_insert(
        users,
        [{"username": "admin", "password_hash": password_hash}],
    )


def downgrade() -> None:
    op.execute("DELETE FROM users WHERE username = 'admin'")
