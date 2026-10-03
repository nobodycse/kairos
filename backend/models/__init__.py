"""SQLAlchemy ORM 模型 —— design.md §2 DDL 的逐字翻译。

表按外键依赖排序；CHECK 约束与索引（含 fault_events 活跃状态的部分索引）
与文档一一对应。Phase 1 只写入 users / fault_events，其余表由后续阶段消费。
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 命名约定：显式声明，保证 Alembic autogenerate 与手写迁移的约束名一致
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _ts_default() -> text:
    return text("now()")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_ts_default()
    )


class Experiment(Base):
    __tablename__ = "experiments"
    __table_args__ = (
        CheckConstraint(
            "fault_type IN ('oom','cpu_overload','pod_crash','image_pull_backoff',"
            "'replica_anomaly','network_latency','node_not_ready')",
            name="fault_type_valid",
        ),
        CheckConstraint(
            "status IN ('created','injecting','injected','finished','cancelled')",
            name="status_valid",
        ),
        Index("idx_experiments_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fault_type: Mapped[str] = mapped_column(Text)
    target_ns: Mapped[str] = mapped_column(Text, server_default=text("'demo'"))
    target_workload: Mapped[str] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'created'"))
    injected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_ts_default()
    )


# fault_events 的活跃状态集合：告警归并的部分索引条件（design.md §2 / §5.3），
# 也是 §1.2 定义的 active 语义（webhook 归并、overview.active_faults、faults 列表共用）
ACTIVE_STATUSES = (
    "detected",
    "diagnosing",
    "awaiting_approval",
    "remediating",
    "verifying",
    "rolling_back",
)
_ACTIVE_STATUSES_SQL = "(" + ",".join(f"'{s}'" for s in ACTIVE_STATUSES) + ")"


class FaultEvent(Base):
    __tablename__ = "fault_events"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('warning','critical')", name="severity_valid"
        ),
        CheckConstraint(
            "status IN ('detected','diagnosing','awaiting_approval','remediating',"
            "'verifying','rolling_back','resolved','failed','closed')",
            name="status_valid",
        ),
        Index("idx_fault_events_status_time", "status", text("detected_at DESC")),
        Index(
            "idx_fault_events_active_ns_wl",
            "namespace",
            "workload",
            postgresql_where=text(f"status IN ({_ACTIVE_STATUSES_SQL})"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(Text)
    alert_name: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    namespace: Mapped[str] = mapped_column(Text)
    workload: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(Text)
    experiment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("experiments.id")
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_ts_default()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mttr_seconds: Mapped[int | None] = mapped_column(Integer)
    rediagnose_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_ts_default()
    )


class Diagnosis(Base):
    __tablename__ = "diagnoses"
    __table_args__ = (
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_valid"),
        Index("idx_diagnoses_event", "fault_event_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fault_event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fault_events.id", ondelete="CASCADE")
    )
    fault_type: Mapped[str] = mapped_column(Text)
    root_cause: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'"))
    confidence: Mapped[float] = mapped_column(Float)
    blast_radius: Mapped[str] = mapped_column(Text)
    suggestion: Mapped[str] = mapped_column(Text)
    llm_model: Mapped[str] = mapped_column(Text)
    iterations: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_ts_default()
    )


class RemediationAction(Base):
    __tablename__ = "remediation_actions"
    __table_args__ = (
        CheckConstraint(
            "action IN ('update_resource_limit','scale_deployment',"
            "'restart_deployment','rollback_deployment','delete_pod')",
            name="action_valid",
        ),
        CheckConstraint(
            "risk_level IN ('low','medium','high','critical')",
            name="risk_level_valid",
        ),
        CheckConstraint(
            "policy IN ('auto','require_approval','forbidden')",
            name="policy_valid",
        ),
        CheckConstraint(
            "status IN ('pending','approved','rejected','executing',"
            "'succeeded','failed','rolled_back')",
            name="status_valid",
        ),
        Index("idx_remediations_event", "fault_event_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fault_event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fault_events.id", ondelete="CASCADE")
    )
    action: Mapped[str] = mapped_column(Text)
    namespace: Mapped[str] = mapped_column(Text)
    target: Mapped[str] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    risk_level: Mapped[str] = mapped_column(Text)
    policy: Mapped[str] = mapped_column(Text)
    approved_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    snapshot: Mapped[dict | None] = mapped_column(JSONB)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_ts_default()
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(
            "result IN ('allowed','denied','success','failure')",
            name="result_valid",
        ),
        Index("idx_audit_logs_time", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    resource: Mapped[str] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    result: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_ts_default()
    )


class ExperimentResult(Base):
    __tablename__ = "experiment_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("experiments.id", ondelete="CASCADE")
    )
    fault_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("fault_events.id")
    )
    detected: Mapped[bool] = mapped_column(Boolean)
    detection_latency_s: Mapped[int | None] = mapped_column(Integer)
    diagnosed_correctly: Mapped[bool | None] = mapped_column(Boolean)
    auto_recovered: Mapped[bool | None] = mapped_column(Boolean)
    mttr_s: Mapped[int | None] = mapped_column(Integer)
    false_action: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_ts_default()
    )


class SystemSetting(Base):
    """系统内键值配置（Phase 2.5 增补：AI 供应商配置等，管理员在系统设置页维护）。

    value 为 JSONB（如 {"base_url", "api_key", "model"}）；API 层永不回明文 key。
    """

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_ts_default()
    )
