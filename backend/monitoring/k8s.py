"""Kubernetes API 客户端（architecture.md §6.2，kubernetes-asyncio）。

workload 推导：pod → ownerReferences(ReplicaSet) → Deployment，
结果缓存 5 分钟（与 design.md §5.3 的 owner 缓存约定一致）。
quantity 解析只覆盖本项目会用到的单位（Ki/Mi/Gi/Ti、K/M/G/T、m、纯数值）。
"""
import time
from datetime import datetime, timezone

from kubernetes_asyncio import client as k8s
from kubernetes_asyncio import config as k8s_config

from core.config import settings
from monitoring.models import DeploymentInfo, EndpointsInfo, K8sEvent, NodeInfo, PodInfo

_WORKLOAD_CACHE_TTL = 300  # 秒，§5.3 owner 缓存


def _parse_quantity(q: str | None) -> float | None:
    """K8s quantity → 数值。CPU 返回核数，内存返回字节数；无法解析返回 None。"""
    if not q:
        return None
    suffixes = {
        "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
        "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12,
    }
    for suffix, factor in suffixes.items():
        if q.endswith(suffix):
            try:
                return float(q[: -len(suffix)]) * factor
            except ValueError:
                return None
    if q.endswith("m"):  # 毫核
        try:
            return float(q[:-1]) / 1000
        except ValueError:
            return None
    try:
        return float(q)
    except ValueError:
        return None


# 公开别名：risk_control 白名单校验 limit 调整幅度时复用（0.5x–4x，§11.1）
parse_quantity = _parse_quantity


class K8sClient:
    def __init__(self, kubeconfig: str):
        self._kubeconfig = kubeconfig
        self._api: k8s.ApiClient | None = None
        self._workload_cache: dict[tuple[str, str], tuple[str | None, float]] = {}

    async def connect(self) -> None:
        await k8s_config.load_kube_config(config_file=self._kubeconfig)
        self._api = k8s.ApiClient()
        self._core = k8s.CoreV1Api(self._api)
        self._apps = k8s.AppsV1Api(self._api)

    async def close(self) -> None:
        if self._api is not None:
            await self._api.close()
            self._api = None

    # ---------- workload 推导 ----------

    async def workload_of_pod(self, ns: str, pod_name: str) -> str | None:
        """公开方法：按 namespace+pod 名推导 workload（webhook 归并用，§5.3）。

        pod 已删除（OOMKilled 后被替换等）返回 None。
        """
        try:
            pod = await self._core.read_namespaced_pod(pod_name, ns)
        except k8s.ApiException as e:
            if e.status == 404:
                return None
            raise
        return await self._workload_of(pod)

    async def _workload_of(self, pod) -> str | None:
        """pod → ownerReferences(ReplicaSet) → Deployment 名；缓存 5 分钟。"""
        refs = pod.metadata.owner_references or []
        if not refs:
            return None
        direct = next((r for r in refs if r.kind == "Deployment"), None)
        if direct:
            return direct.name
        rs = next((r for r in refs if r.kind == "ReplicaSet"), None)
        if rs is None:
            # DaemonSet / StatefulSet / Job：取直属 owner 名（无 Deployment 时可能为 None）
            owned = next((r for r in refs if r.kind in ("StatefulSet", "DaemonSet")), None)
            return owned.name if owned else None
        ns = pod.metadata.namespace
        cached = self._workload_cache.get((ns, rs.name))
        if cached and cached[1] > time.time():
            return cached[0]
        workload: str | None = None
        try:
            rs_obj = await self._apps.read_namespaced_replica_set(rs.name, ns)
            dep = next(
                (r for r in (rs_obj.metadata.owner_references or []) if r.kind == "Deployment"),
                None,
            )
            workload = dep.name if dep else None
        except k8s.ApiException:
            pass
        self._workload_cache[(ns, rs.name)] = (workload, time.time() + _WORKLOAD_CACHE_TTL)
        return workload

    # ---------- Pod ----------

    async def list_pods(
        self,
        ns: str | None = None,
        label_selector: str | None = None,
        field_selector: str | None = None,
    ) -> list[PodInfo]:
        if ns:
            resp = await self._core.list_namespaced_pod(
                ns, label_selector=label_selector, field_selector=field_selector
            )
        else:
            resp = await self._core.list_pod_for_all_namespaces(
                label_selector=label_selector, field_selector=field_selector
            )
        now = time.time()
        out: list[PodInfo] = []
        for pod in resp.items:
            st = pod.status
            phase = st.phase or "Unknown"
            cs = st.container_statuses or []
            waiting_reason = next(
                (c.state.waiting.reason for c in cs if c.state and c.state.waiting and c.state.waiting.reason),
                None,
            )
            # 运维视角：有 waiting reason（CrashLoopBackOff/ImagePullBackOff/...）优先于 phase
            status = waiting_reason or phase
            ready = all(c.ready for c in cs) if cs else phase == "Running"
            restarts = sum(c.restart_count or 0 for c in cs)
            age = max(0, int(now - pod.metadata.creation_timestamp.replace(tzinfo=timezone.utc).timestamp()))
            limits = 0
            for c in pod.spec.containers:
                limits += _parse_quantity((c.resources.limits or {}).get("memory")) or 0
            out.append(
                PodInfo(
                    namespace=pod.metadata.namespace,
                    name=pod.metadata.name,
                    workload=await self._workload_of(pod),
                    node=pod.spec.node_name,
                    phase=phase,
                    status=status,
                    ready=ready,
                    restarts=restarts,
                    age_seconds=age,
                    memory_limit_bytes=limits or None,
                    pod_ip=st.pod_ip or None,
                )
            )
        return out

    async def get_pod(self, ns: str, name: str) -> PodInfo | None:
        try:
            pod = await self._core.read_namespaced_pod(name, ns)
        except k8s.ApiException as e:
            if e.status == 404:
                return None
            raise
        return (await self.list_pods(ns, field_selector=f"metadata.name={name}"))[0] if pod else None

    # ---------- Pod 写操作（Phase 3 fault-lab 专用，architecture.md §10） ----------

    async def create_namespaced_pod(self, ns: str, body: dict) -> str:
        """创建 Pod（fault-lab stress-ng 压力 Pod），返回 Pod 名。"""
        resp = await self._core.create_namespaced_pod(namespace=ns, body=body)
        return resp.metadata.name

    async def delete_namespaced_pod(self, ns: str, name: str) -> None:
        """删除 Pod（实验收尾清理）；不存在视为已删除。"""
        try:
            await self._core.delete_namespaced_pod(name=name, namespace=ns)
        except k8s.ApiException as e:
            if e.status != 404:
                raise

    async def describe_pod(self, ns: str, name: str) -> dict | None:
        """status + conditions + 容器状态聚合（Phase 2 Agent 工具 get_pod 的底层）。"""
        try:
            pod = await self._core.read_namespaced_pod(name, ns)
        except k8s.ApiException as e:
            if e.status == 404:
                return None
            raise
        st = pod.status
        return {
            "namespace": ns,
            "name": name,
            "phase": st.phase,
            "conditions": [
                {"type": c.type, "status": c.status, "reason": c.reason}
                for c in (st.conditions or [])
            ],
            "containers": [
                {
                    "name": c.name,
                    "ready": c.ready,
                    "restarts": c.restart_count,
                    "state": {
                        kind: {"reason": getattr(getattr(c.state, kind), "reason", None),
                               "exit_code": getattr(getattr(c.state, kind), "exit_code", None)}
                        for kind in ("waiting", "running", "terminated")
                        if getattr(c.state, kind)
                    },
                }
                for c in (st.container_statuses or [])
            ],
        }

    # ---------- Deployment ----------

    @staticmethod
    def _to_deployment_info(dep) -> DeploymentInfo:
        containers = dep.spec.template.spec.containers or []
        limits = containers[0].resources.limits if containers else None
        return DeploymentInfo(
            namespace=dep.metadata.namespace,
            name=dep.metadata.name,
            replicas=dep.spec.replicas or 0,
            ready_replicas=dep.status.ready_replicas or 0,
            image=containers[0].image if containers else "",
            cpu_limit=(limits or {}).get("cpu"),
            memory_limit=(limits or {}).get("memory"),
            labels=dict(dep.metadata.labels or {}),
            container=containers[0].name if containers else None,
        )

    async def get_deployment(self, ns: str, name: str) -> DeploymentInfo | None:
        try:
            dep = await self._apps.read_namespaced_deployment(name, ns)
        except k8s.ApiException as e:
            if e.status == 404:
                return None
            raise
        return self._to_deployment_info(dep)

    async def patch_deployment(self, ns: str, name: str, body: dict) -> DeploymentInfo:
        """patch Deployment（§6.5：所有动作经 patch 而非 replace）并返回最新状态。

        404 向上抛（executor 侧在 patch 前已 get_deployment 确认存在）。
        """
        await self._apps.patch_namespaced_deployment(name=name, namespace=ns, body=body)
        dep = await self._apps.read_namespaced_deployment(name, ns)
        return self._to_deployment_info(dep)

    async def list_deployments(self, ns: str | None = None) -> list[DeploymentInfo]:
        if ns:
            resp = await self._apps.list_namespaced_deployment(ns)
        else:
            resp = await self._apps.list_deployment_for_all_namespaces()
        return [self._to_deployment_info(d) for d in resp.items]

    # ---------- Events ----------

    async def list_events(
        self, ns: str | None = None, field_selector: str | None = None
    ) -> list[K8sEvent]:
        if ns:
            resp = await self._core.list_namespaced_event(ns, field_selector=field_selector)
        else:
            resp = await self._core.list_event_for_all_namespaces(field_selector=field_selector)
        out: list[K8sEvent] = []
        for ev in resp.items:
            io = ev.involved_object
            out.append(
                K8sEvent(
                    namespace=ev.metadata.namespace or "",
                    type=ev.type or "Normal",
                    reason=ev.reason or "",
                    object=f"{io.namespace}/{io.name}" if io else "",
                    message=ev.message or "",
                    count=ev.count or 0,
                    last_seen=(ev.last_timestamp or ev.event_time),
                )
            )
        return out

    # ---------- Service ----------

    async def get_service_endpoints(self, ns: str, name: str) -> EndpointsInfo | None:
        """Service 的 Endpoints 地址计数（Phase 2 工具 get_service_status 底层）。"""
        try:
            eps = await self._core.read_namespaced_endpoints(name, ns)
        except k8s.ApiException as e:
            if e.status == 404:
                return None
            raise
        subsets = eps.subsets or []
        ready = sum(len(s.addresses or []) for s in subsets)
        not_ready = sum(len(s.not_ready_addresses or []) for s in subsets)
        return EndpointsInfo(
            namespace=ns, service=name, ready_addresses=ready, not_ready_addresses=not_ready
        )

    # ---------- Node ----------

    async def get_node(self, name: str) -> NodeInfo | None:
        try:
            node = await self._core.read_node(name)
        except k8s.ApiException as e:
            if e.status == 404:
                return None
            raise
        return (await self._to_node_infos([node]))[0]

    async def list_nodes(self) -> list[NodeInfo]:
        resp = await self._core.list_node()
        return await self._to_node_infos(resp.items)

    async def _to_node_infos(self, nodes) -> list[NodeInfo]:
        all_pods = await self._core.list_pod_for_all_namespaces()
        by_node: dict[str, int] = {}
        for pod in all_pods.items:
            if pod.spec.node_name:
                by_node[pod.spec.node_name] = by_node.get(pod.spec.node_name, 0) + 1
        out: list[NodeInfo] = []
        for node in nodes:
            conds = {c.type: c.status for c in (node.status.conditions or [])}
            roles = ",".join(
                label.split("/", 1)[1]
                for label in (node.metadata.labels or {})
                if label.startswith("node-role.kubernetes.io/")
            )
            alloc = node.status.allocatable or {}
            out.append(
                NodeInfo(
                    name=node.metadata.name,
                    status="Ready" if conds.get("Ready") == "True" else "NotReady",
                    roles=roles or "worker",
                    version=node.status.node_info.kubelet_version if node.status.node_info else "",
                    cpu_alloc_cores=_parse_quantity(alloc.get("cpu")) or 0.0,
                    memory_alloc_bytes=int(_parse_quantity(alloc.get("memory")) or 0),
                    pods_count=by_node.get(node.metadata.name, 0),
                )
            )
        return out
