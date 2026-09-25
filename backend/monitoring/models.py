"""monitoring 客户端的返回模型（architecture.md §6.2：客户端只返回 pydantic 模型）。

K8s 侧信息（PodInfo/DeploymentInfo/K8sEvent/NodeInfo）与 Prometheus 侧的
用量数据在 cluster 路由层合并（design.md §3.3：cpu/memory 用量来自 cAdvisor）。
"""
from datetime import datetime

from pydantic import BaseModel


class Sample(BaseModel):
    """Prometheus 即时查询的单个样本。metric 保留原始 label 集合。"""

    metric: dict[str, str]
    value: float
    timestamp: float


class Series(BaseModel):
    """Prometheus 范围查询单条时间序列，values 为 (unix_ts, value) 对。"""

    metric: dict[str, str]
    values: list[tuple[float, float]]


class PodInfo(BaseModel):
    namespace: str
    name: str
    workload: str | None  # ownerReferences 推导（ReplicaSet → Deployment）
    node: str | None
    phase: str  # Running / Pending / Succeeded / Failed
    status: str  # 运维视角：container waiting reason 优先于 phase
    ready: bool
    restarts: int
    age_seconds: int
    memory_limit_bytes: int | None


class DeploymentInfo(BaseModel):
    namespace: str
    name: str
    replicas: int
    ready_replicas: int
    image: str
    cpu_limit: str | None  # K8s quantity 原样（"500m"）
    memory_limit: str | None  # K8s quantity 原样（"512Mi"）


class K8sEvent(BaseModel):
    namespace: str
    type: str  # Normal / Warning
    reason: str
    object: str  # "demo/payment-service-7d9f6b8c5-x2k4l"
    message: str
    count: int
    last_seen: datetime | None


class NodeInfo(BaseModel):
    name: str
    status: str  # Ready / NotReady
    roles: str  # "control-plane,etcd"（node-role.kubernetes.io/* 标签推导）
    version: str  # kubelet 版本
    cpu_alloc_cores: float
    memory_alloc_bytes: int
    pods_count: int
