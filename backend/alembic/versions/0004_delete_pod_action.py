"""增补迁移：remediation_actions.action CHECK 追加 'delete_pod'（Phase 4 自愈闭环）。

本项目首个对存量表的真迁移（0001 基线 / 0002 种子 / 0003 建表均为"从无到
有"）：DROP 后按新取值集重建同名约束。存量数据无 delete_pod 行（动作集此前
不含该值），重建必然成功；**downgrade 前需先清理 action='delete_pod' 的行**
（否则原 CHECK 违例）——生产惯例是先删数据再降级。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-03
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# 与 models/__init__.py 的 action_valid CheckConstraint 逐字对齐（含 0004 追加值）
_NEW_EXPR = (
    "action IN ('update_resource_limit','scale_deployment',"
    "'restart_deployment','rollback_deployment','delete_pod')"
)
_OLD_EXPR = (
    "action IN ('update_resource_limit','scale_deployment',"
    "'restart_deployment','rollback_deployment')"
)
_CONSTRAINT = "ck_remediation_actions_action_valid"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "remediation_actions", type_="check")
    op.create_check_constraint(_CONSTRAINT, "remediation_actions", _NEW_EXPR)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "remediation_actions", type_="check")
    op.create_check_constraint(_CONSTRAINT, "remediation_actions", _OLD_EXPR)
