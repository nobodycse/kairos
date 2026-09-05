"""Phase 0 stub 假数据。

统一使用 docs/design.md §3/§4 的同一个故事：demo namespace 的
payment-service 发生 OOM。字段与文档示例逐字段一致，前端可拿
文档与线上 /docs 交叉核对。Phase 1 接真实数据源后本模块移除。
"""
import itertools
import threading

# ---------- GET /cluster/overview（design.md §3.2） ----------
CLUSTER_OVERVIEW = {
    "nodes": {"total": 1, "ready": 1},
    "pods": {"total": 18, "running": 16, "pending": 1, "failed": 1},
    "deployments": {"total": 5, "available": 4},
    "active_faults": 1,
    "resources": {"cpu_usage_ratio": 0.31, "memory_usage_ratio": 0.52},
    "qps": 42.5,
    "error_rate": 0.012,
    "p95_latency": 0.24,
}

# ---------- GET /cluster/pods（design.md §3.3，条目结构照抄示例） ----------
PODS = [
    {
        "namespace": "demo",
        "name": "payment-service-7d9f6b8c5-x2k4l",
        "workload": "payment-service",
        "node": "kairos-node1",
        "status": "CrashLoopBackOff",
        "ready": False,
        "restarts": 12,
        "age_seconds": 3600,
        "cpu_usage_cores": 0.05,
        "memory_usage_bytes": 52428800,
        "memory_limit_bytes": 536870912,
    },
    {
        "namespace": "demo",
        "name": "payment-service-7d9f6b8c5-q8v7m",
        "workload": "payment-service",
        "node": "kairos-node1",
        "status": "Running",
        "ready": True,
        "restarts": 3,
        "age_seconds": 3600,
        "cpu_usage_cores": 0.08,
        "memory_usage_bytes": 201326592,
        "memory_limit_bytes": 536870912,
    },
    {
        "namespace": "demo",
        "name": "order-service-5c6d7e8f9-l3m1n",
        "workload": "order-service",
        "node": "kairos-node1",
        "status": "Running",
        "ready": True,
        "restarts": 0,
        "age_seconds": 86400,
        "cpu_usage_cores": 0.12,
        "memory_usage_bytes": 157286400,
        "memory_limit_bytes": 536870912,
    },
    {
        "namespace": "demo",
        "name": "inventory-service-84f5a6b7c9-p4q5r",
        "workload": "inventory-service",
        "node": "kairos-node1",
        "status": "Running",
        "ready": True,
        "restarts": 0,
        "age_seconds": 86400,
        "cpu_usage_cores": 0.06,
        "memory_usage_bytes": 94371840,
        "memory_limit_bytes": 268435456,
    },
    {
        "namespace": "monitoring",
        "name": "kube-state-metrics-6d7b8c9df-x1y2z",
        "workload": "kube-state-metrics",
        "node": "kairos-node1",
        "status": "Running",
        "ready": True,
        "restarts": 0,
        "age_seconds": 172800,
        "cpu_usage_cores": 0.02,
        "memory_usage_bytes": 31457280,
        "memory_limit_bytes": 268435456,
    },
]

# ---------- GET /cluster/nodes（design.md §3.4） ----------
NODES = [
    {
        "name": "kairos-node1",
        "status": "Ready",
        "roles": "control-plane,etcd",
        "version": "v1.30.4+k3s1",
        "cpu_alloc_cores": 4,
        "memory_alloc_bytes": 8035227648,
        "cpu_usage_ratio": 0.31,
        "memory_usage_ratio": 0.52,
        "pods_count": 18,
    }
]

# ---------- GET /cluster/deployments（design.md §3.4） ----------
DEPLOYMENTS = [
    {
        "namespace": "demo",
        "name": "payment-service",
        "replicas": 2,
        "ready_replicas": 1,
        "image": "kairos/demo-app:latest",
        "cpu_limit": "500m",
        "memory_limit": "512Mi",
    },
    {
        "namespace": "demo",
        "name": "order-service",
        "replicas": 2,
        "ready_replicas": 2,
        "image": "kairos/demo-app:latest",
        "cpu_limit": "500m",
        "memory_limit": "512Mi",
    },
    {
        "namespace": "demo",
        "name": "inventory-service",
        "replicas": 1,
        "ready_replicas": 1,
        "image": "kairos/demo-app:latest",
        "cpu_limit": "300m",
        "memory_limit": "256Mi",
    },
]

# ---------- GET /cluster/events（design.md §3.4） ----------
EVENTS = [
    {
        "namespace": "demo",
        "type": "Warning",
        "reason": "BackOff",
        "object": "demo/payment-service-7d9f6b8c5-x2k4l",
        "message": "Back-off restarting failed container",
        "count": 9,
        "last_seen": "2026-09-04T11:58:40Z",
    },
    {
        "namespace": "demo",
        "type": "Warning",
        "reason": "OOMKilled",
        "object": "demo/payment-service-7d9f6b8c5-x2k4l",
        "message": "Memory cgroup out of memory",
        "count": 2,
        "last_seen": "2026-09-04T11:52:09Z",
    },
    {
        "namespace": "demo",
        "type": "Warning",
        "reason": "FailedScheduling",
        "object": "demo/inventory-service-84f5a6b7c9",
        "message": "0/1 nodes are available: 1 Insufficient memory",
        "count": 1,
        "last_seen": "2026-09-04T10:31:02Z",
    },
]

# ---------- 故障事件（design.md §3.5 / §3.6） ----------
# 42 = 主故事（awaiting_approval）；43/44 供状态分支测试：
# 43 diagnosing（触发 diagnose → 409 正在诊断中），44 resolved（→ 409 已结束）
_FAULT_42 = {
    "id": 42,
    "fingerprint": "e5f0a1b2c3d4e5f6",
    "alert_name": "PodOOMKilled",
    "severity": "critical",
    "namespace": "demo",
    "workload": "payment-service",
    "labels": {
        "alertname": "PodOOMKilled",
        "namespace": "demo",
        "pod": "payment-service-7d9f6b8c5-x2k4l",
    },
    "status": "awaiting_approval",
    "detected_at": "2026-09-04T11:52:10Z",
    "resolved_at": None,
    "mttr_seconds": None,
    "rediagnose_count": 0,
    "experiment_id": None,
    "diagnosis": {
        "id": 57,
        "fault_type": "OOM",
        "root_cause": "容器内存不足：memory limit 为 512Mi，工作集持续超过 95%",
        "evidence": [
            {
                "source": "k8s_api",
                "tool": "get_pod_status",
                "summary": "最近 10 分钟重启 12 次",
                "data": {"restarts": 12, "window": "10m"},
            },
            {
                "source": "events",
                "tool": "get_k8s_events",
                "summary": "出现 OOMKilled 事件 x2",
                "data": {"reasons": ["OOMKilled"], "count": 2},
            },
            {
                "source": "prometheus",
                "tool": "get_metrics",
                "summary": "内存使用率持续 >95%",
                "data": {"metric": "memory_usage_ratio", "max": 0.97},
            },
            {
                "source": "loki",
                "tool": "get_pod_logs",
                "summary": "日志出现 out of memory",
                "data": {"lines_matched": 8},
            },
        ],
        "confidence": 0.94,
        "blast_radius": "demo/payment-service 全部 2 副本，支付接口受影响",
        "suggestion": "将 memory limit 从 512Mi 调整至 1Gi",
        "llm_model": "deepseek-chat",
        "iterations": 5,
        "created_at": "2026-09-04T11:55:02Z",
    },
    "remediations": [
        {
            "id": 31,
            "action": "update_resource_limit",
            "namespace": "demo",
            "target": "payment-service",
            "params": {"container": "app", "memory_limit": "1Gi"},
            "risk_level": "medium",
            "policy": "require_approval",
            "status": "pending",
            "approved_by": None,
            "snapshot": {"memory_limit": "512Mi"},
            "executed_at": None,
        }
    ],
}

_FAULT_43 = {
    "id": 43,
    "fingerprint": "a1b2c3d4e5f6a7b8",
    "alert_name": "PodCrashLooping",
    "severity": "warning",
    "namespace": "demo",
    "workload": "order-service",
    "labels": {
        "alertname": "PodCrashLooping",
        "namespace": "demo",
        "pod": "order-service-5c6d7e8f9-l3m1n",
    },
    "status": "diagnosing",
    "detected_at": "2026-09-04T12:05:00Z",
    "resolved_at": None,
    "mttr_seconds": None,
    "rediagnose_count": 0,
    "experiment_id": None,
    "diagnosis": None,
    "remediations": [],
}

_FAULT_44 = {
    "id": 44,
    "fingerprint": "b2c3d4e5f6a7b8c9",
    "alert_name": "HTTPLatencyHigh",
    "severity": "warning",
    "namespace": "demo",
    "workload": "inventory-service",
    "labels": {
        "alertname": "HTTPLatencyHigh",
        "namespace": "demo",
        "pod": "inventory-service-84f5a6b7c9-p4q5r",
    },
    "status": "resolved",
    "detected_at": "2026-09-04T09:14:30Z",
    "resolved_at": "2026-09-04T09:18:30Z",
    "mttr_seconds": 240,
    "rediagnose_count": 0,
    "experiment_id": 6,
    "diagnosis": {
        "id": 52,
        "fault_type": "HighLatency",
        "root_cause": "上游依赖抖动导致 /pay 延迟升高",
        "evidence": [
            {
                "source": "prometheus",
                "tool": "get_metrics",
                "summary": "P95 延迟 3.2s 持续 5 分钟",
                "data": {"metric": "p95_latency_seconds", "max": 3.2},
            }
        ],
        "confidence": 0.81,
        "blast_radius": "demo/inventory-service 单副本",
        "suggestion": "观察上游恢复情况，暂不执行变更",
        "llm_model": "deepseek-chat",
        "iterations": 3,
        "created_at": "2026-09-04T09:16:00Z",
    },
    "remediations": [],
}

FAULTS: dict[int, dict] = {42: _FAULT_42, 43: _FAULT_43, 44: _FAULT_44}

# 列表条目投影字段（design.md §3.5）
_LIST_FIELDS = (
    "id",
    "alert_name",
    "severity",
    "namespace",
    "workload",
    "status",
    "detected_at",
    "resolved_at",
    "mttr_seconds",
)

# active = 未到终态的六态（design.md §1.2）
_ACTIVE_STATES = {
    "detected",
    "diagnosing",
    "awaiting_approval",
    "remediating",
    "verifying",
    "rolling_back",
}


def list_faults(status: str | None = None, namespace: str | None = None) -> list[dict]:
    items = [
        {k: f[k] for k in _LIST_FIELDS}
        for f in FAULTS.values()
        if (status is None
            or (status == "active" and f["status"] in _ACTIVE_STATES)
            or f["status"] == status)
        and (namespace is None or f["namespace"] == namespace)
    ]
    return sorted(items, key=lambda f: f["detected_at"], reverse=True)

# ---------- SSE 演示序列（design.md §4） ----------
# snapshot → agent_step 成对推送 → status_changed → verification_progress → 心跳
# verification_progress 在 awaiting_approval 之后推送仅为演示该事件类型，
# 真实时序见 §5：批准 → remediating → verifying 才有采样点

SSE_SNAPSHOT = {
    "fault_event_id": 42,
    "status": "diagnosing",
    "alert_name": "PodOOMKilled",
    "detected_at": "2026-09-04T11:52:10Z",
    "recent_steps": [],
}

SSE_DEMO_STEPS = [
    {
        "iteration": 1,
        "phase": "collect_evidence",
        "step": "tool_start",
        "tool": "get_pod_status",
        "args": {"namespace": "demo", "name": "payment-service-7d9f6b8c5-x2k4l"},
        "at": "2026-09-04T11:52:20Z",
    },
    {
        "iteration": 1,
        "phase": "collect_evidence",
        "step": "tool_end",
        "tool": "get_pod_status",
        "duration_ms": 180,
        "evidence_summary": "最近 10 分钟重启 12 次",
        "at": "2026-09-04T11:52:21Z",
    },
    {
        "iteration": 2,
        "phase": "collect_evidence",
        "step": "tool_start",
        "tool": "get_metrics",
        "args": {"metric": "memory_usage_ratio", "window": "10m"},
        "at": "2026-09-04T11:53:00Z",
    },
    {
        "iteration": 2,
        "phase": "collect_evidence",
        "step": "tool_end",
        "tool": "get_metrics",
        "duration_ms": 240,
        "evidence_summary": "内存使用率持续 >95%",
        "at": "2026-09-04T11:53:01Z",
    },
    {
        "iteration": 3,
        "phase": "collect_evidence",
        "step": "tool_start",
        "tool": "get_k8s_events",
        "args": {"namespace": "demo", "pod": "payment-service-7d9f6b8c5-x2k4l"},
        "at": "2026-09-04T11:53:40Z",
    },
    {
        "iteration": 3,
        "phase": "collect_evidence",
        "step": "tool_end",
        "tool": "get_k8s_events",
        "duration_ms": 350,
        "evidence_summary": "发现 OOMKilled 事件 x2",
        "at": "2026-09-04T11:53:41Z",
    },
]

SSE_STATUS_CHANGED = {
    "fault_event_id": 42,
    "from": "diagnosing",
    "to": "awaiting_approval",
    "reason": "方案生成，风险等级 medium，需人工确认",
    "at": "2026-09-04T11:55:05Z",
}

SSE_VERIFICATION = {
    "fault_event_id": 42,
    "sample": 4,
    "of": 6,
    "checks": {
        "pod_ready": True,
        "no_restarts": True,
        "error_rate": {"value": 0.03, "ok": True},
        "p95_latency": {"value": 1.2, "ok": False},
        "logs_clean": True,
    },
    "all_passed": False,
    "at": "2026-09-04T12:01:00Z",
}

# ---------- 实验室（design.md §3.9–§3.11） ----------

_EXPERIMENT_8 = {
    "id": 8,
    "fault_type": "oom",
    "target_ns": "demo",
    "target_workload": "payment-service",
    "params": {"memory_limit": "128Mi"},
    "status": "finished",
    "injected_at": "2026-09-04T12:10:05Z",
    "created_at": "2026-09-04T12:10:00Z",
    # report 附加字段（design.md §3.11）
    "fault_event_id": 45,
    "result": {
        "detected": True,
        "detection_latency_s": 95,
        "diagnosed_correctly": True,
        "auto_recovered": True,
        "mttr_s": 240,
        "false_action": False,
        "notes": "根因判定 OOM 正确，limit 调整后一次通过验证",
    },
}

_experiments: dict[int, dict] = {8: _EXPERIMENT_8}
_id_counter = itertools.count(100)
_lock = threading.Lock()


def create_experiment(fault_type: str, target_workload: str, params: dict) -> dict:
    exp = {
        "id": next(_id_counter),
        "fault_type": fault_type,
        "target_ns": "demo",
        "target_workload": target_workload,
        "params": params,
        "status": "created",
        "injected_at": None,
        "created_at": "2026-09-04T12:00:00Z",
        "fault_event_id": None,
        "result": None,
    }
    with _lock:
        _experiments[exp["id"]] = exp
    return exp


def get_experiment(experiment_id: int) -> dict | None:
    with _lock:
        return _experiments.get(experiment_id)


def mark_injected(experiment_id: int) -> None:
    with _lock:
        exp = _experiments.get(experiment_id)
        if exp and exp["status"] == "created":
            exp["status"] = "injected"

# ---------- GET /reports/summary（design.md §3.12） ----------
REPORTS_SUMMARY = {
    "total_experiments": 10,
    "overall": {
        "diagnosis_accuracy": 0.9,
        "recovery_rate": 0.7,
        "avg_mttr_s": 285,
        "false_action_rate": 0.0,
    },
    "by_fault_type": [
        {
            "fault_type": "oom",
            "runs": 4,
            "diagnosis_accuracy": 1.0,
            "recovery_rate": 0.75,
            "avg_mttr_s": 240,
            "false_action_rate": 0.0,
        },
        {
            "fault_type": "cpu_overload",
            "runs": 3,
            "diagnosis_accuracy": 0.67,
            "recovery_rate": 0.67,
            "avg_mttr_s": 310,
            "false_action_rate": 0.0,
        },
        {
            "fault_type": "pod_crash",
            "runs": 3,
            "diagnosis_accuracy": 1.0,
            "recovery_rate": 0.67,
            "avg_mttr_s": 305,
            "false_action_rate": 0.0,
        },
    ],
}
