"""基线迁移：design.md §2 全量建表（8 张表 + 索引）。

schema 已在设计评审定稿，一次建全避免后续 churn；
Phase 1 只写入 users / fault_events，其余表由后续阶段消费。

Revision ID: 0001
Revises:
Create Date: 2026-09-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _ts_now() -> sa.text:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.Text(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_ts_now(),
            nullable=False,
        ),
    )

    op.create_table(
        "experiments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("fault_type", sa.Text(), nullable=False),
        sa.Column("target_ns", sa.Text(), nullable=False, server_default=sa.text("'demo'")),
        sa.Column("target_workload", sa.Text(), nullable=False),
        sa.Column("params", JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'created'")),
        sa.Column("injected_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_ts_now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "fault_type IN ('oom','cpu_overload','pod_crash','image_pull_backoff',"
            "'replica_anomaly','network_latency','node_not_ready')",
            name="ck_experiments_fault_type_valid",
        ),
        sa.CheckConstraint(
            "status IN ('created','injecting','injected','finished','cancelled')",
            name="ck_experiments_status_valid",
        ),
    )
    op.create_index("idx_experiments_status", "experiments", ["status"])

    op.create_table(
        "fault_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("alert_name", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("workload", sa.Text()),
        sa.Column("labels", JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("experiment_id", sa.BigInteger(), sa.ForeignKey("experiments.id")),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=_ts_now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("mttr_seconds", sa.Integer()),
        sa.Column("rediagnose_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=_ts_now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "severity IN ('warning','critical')",
            name="ck_fault_events_severity_valid",
        ),
        sa.CheckConstraint(
            "status IN ('detected','diagnosing','awaiting_approval','remediating',"
            "'verifying','rolling_back','resolved','failed','closed')",
            name="ck_fault_events_status_valid",
        ),
    )
    op.create_index(
        "idx_fault_events_status_time",
        "fault_events",
        ["status", sa.text("detected_at DESC")],
    )
    # 告警归并：查同 namespace+workload 的活跃事件（design.md §5.3）
    op.create_index(
        "idx_fault_events_active_ns_wl",
        "fault_events",
        ["namespace", "workload"],
        postgresql_where=sa.text(
            "status IN ('detected','diagnosing','awaiting_approval',"
            "'remediating','verifying','rolling_back')"
        ),
    )

    op.create_table(
        "diagnoses",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "fault_event_id",
            sa.BigInteger(),
            sa.ForeignKey("fault_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fault_type", sa.Text(), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=False),
        sa.Column("evidence", JSONB(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("blast_radius", sa.Text(), nullable=False),
        sa.Column("suggestion", sa.Text(), nullable=False),
        sa.Column("llm_model", sa.Text(), nullable=False),
        sa.Column("iterations", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_ts_now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence BETWEEN 0 AND 1", name="ck_diagnoses_confidence_valid"
        ),
    )
    op.create_index("idx_diagnoses_event", "diagnoses", ["fault_event_id"])

    op.create_table(
        "remediation_actions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "fault_event_id",
            sa.BigInteger(),
            sa.ForeignKey("fault_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("params", JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("risk_level", sa.Text(), nullable=False),
        sa.Column("policy", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("snapshot", JSONB()),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_ts_now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('update_resource_limit','scale_deployment',"
            "'restart_deployment','rollback_deployment')",
            name="ck_remediation_actions_action_valid",
        ),
        sa.CheckConstraint(
            "risk_level IN ('low','medium','high','critical')",
            name="ck_remediation_actions_risk_level_valid",
        ),
        sa.CheckConstraint(
            "policy IN ('auto','require_approval','forbidden')",
            name="ck_remediation_actions_policy_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','executing',"
            "'succeeded','failed','rolled_back')",
            name="ck_remediation_actions_status_valid",
        ),
    )
    op.create_index("idx_remediations_event", "remediation_actions", ["fault_event_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("params", JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("detail", JSONB()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_ts_now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "result IN ('allowed','denied','success','failure')",
            name="ck_audit_logs_result_valid",
        ),
    )
    op.create_index("idx_audit_logs_time", "audit_logs", [sa.text("created_at DESC")])

    op.create_table(
        "experiment_results",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "experiment_id",
            sa.BigInteger(),
            sa.ForeignKey("experiments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fault_event_id", sa.BigInteger(), sa.ForeignKey("fault_events.id")),
        sa.Column("detected", sa.Boolean(), nullable=False),
        sa.Column("detection_latency_s", sa.Integer()),
        sa.Column("diagnosed_correctly", sa.Boolean()),
        sa.Column("auto_recovered", sa.Boolean()),
        sa.Column("mttr_s", sa.Integer()),
        sa.Column("false_action", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("notes", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_ts_now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("experiment_results")
    op.drop_table("audit_logs")
    op.drop_index("idx_remediations_event", table_name="remediation_actions")
    op.drop_table("remediation_actions")
    op.drop_index("idx_diagnoses_event", table_name="diagnoses")
    op.drop_table("diagnoses")
    op.drop_index("idx_fault_events_active_ns_wl", table_name="fault_events")
    op.drop_index("idx_fault_events_status_time", table_name="fault_events")
    op.drop_table("fault_events")
    op.drop_index("idx_experiments_status", table_name="experiments")
    op.drop_table("experiments")
    op.drop_table("users")
