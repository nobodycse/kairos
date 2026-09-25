"""集群只读接口（architecture.md §6.1，design.md §3.2–§3.2.1）。

数据源：K8s API（结构数据，硬依赖，失败 502）+ Prometheus（用量/业务指标，
软依赖，失败时相关字段显式 null，见 §3.2 降级语义）。
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import require_user
from monitoring import clients
from monitoring.models import PodInfo

router = APIRouter(prefix="/cluster", tags=["cluster"])

# 与 §3.2 快照值同源的 PromQL（demo-app 指标见 §6.1）
_QPS = 'sum(rate(demo_http_requests_total[5m]))'
_ERROR_RATE_5XX = 'sum(rate(demo_http_requests_total{status=~"5.."}[5m]))'
_ERROR_RATE_TOTAL = 'sum(rate(demo_http_requests_total[5m]))'
_P95 = (
    'histogram_quantile(0.95, '
    'sum by (le) (rate(demo_http_request_duration_seconds_bucket[5m])))'
)
_CPU_RATIO = (
    'sum(rate(container_cpu_usage_seconds_total{container!="",container!="POD"}[5m]))'
    ' / clamp_min(sum(kube_node_status_allocatable{resource="cpu"}), 1e-9)'
)
_MEM_RATIO = (
    'sum(container_memory_working_set_bytes{container!="",container!="POD"})'
    ' / clamp_min(sum(kube_node_status_allocatable{resource="memory"}), 1e-9)'
)
_POD_CPU = (
    'sum by (namespace, pod) (rate(container_cpu_usage_seconds_total'
    '{container!="",container!="POD"}[5m]))'
)
_POD_MEM = (
    'sum by (namespace, pod) (container_memory_working_set_bytes'
    '{container!="",container!="POD"})'
)


def _paginate(items: list, page: int, page_size: int) -> dict:
    return {
        "items": items[(page - 1) * page_size : page * page_size],
        "total": len(items),
        "page": page,
        "page_size": page_size,
    }


async def _scalar(expr: str) -> float | None:
    """单值查询：Prometheus 失败/无数据返回 None（软依赖降级）。"""
    try:
        samples = await clients.prometheus.query(expr)
    except Exception:  # noqa: BLE001  httpx 网络错误一律降级
        return None
    return samples[0].value if samples else None


async def _error_rate() -> float | None:
    """错误率特殊处理：区分"不可达"（null）与"可达但无 5xx 样本"（0）。"""
    try:
        num_s = await clients.prometheus.query(_ERROR_RATE_5XX)
        den_s = await clients.prometheus.query(_ERROR_RATE_TOTAL)
    except Exception:  # noqa: BLE001  软依赖降级
        return None
    if not den_s:  # 连总请求量都没有 → 无法计算
        return None
    num = num_s[0].value if num_s else 0.0
    return num / max(den_s[0].value, 1e-9)


async def _k8s_pods(ns: str | None) -> list[PodInfo]:
    try:
        return await clients.k8s.list_pods(ns)
    except Exception as e:  # noqa: BLE001  硬依赖，失败明确报错
        raise HTTPException(status_code=502, detail=f"K8s API 不可达: {e}") from e


@router.get("/overview", summary="集群总览快照（§3.2）")
async def overview(_: str = Depends(require_user)):
    try:
        pods = await clients.k8s.list_pods()
        deps = await clients.k8s.list_deployments()
        nodes = await clients.k8s.list_nodes()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"K8s API 不可达: {e}") from e

    return {
        "nodes": {
            "total": len(nodes),
            "ready": sum(1 for n in nodes if n.status == "Ready"),
        },
        "pods": {
            "total": len(pods),
            "running": sum(1 for p in pods if p.phase == "Running"),
            "pending": sum(1 for p in pods if p.phase == "Pending"),
            "failed": sum(1 for p in pods if p.phase == "Failed"),
        },
        "deployments": {
            "total": len(deps),
            "available": sum(1 for d in deps if d.ready_replicas >= d.replicas),
        },
        # Phase 1 三阶段接 webhook 落库后改为查 fault_events（§5.3 六态 active）
        "active_faults": 0,
        "resources": {
            "cpu_usage_ratio": await _scalar(_CPU_RATIO),
            "memory_usage_ratio": await _scalar(_MEM_RATIO),
        },
        "qps": await _scalar(_QPS),
        "error_rate": await _error_rate(),
        "p95_latency": await _scalar(_P95),
    }


@router.get("/pods", summary="Pod 列表（§3.3）")
async def list_pods(
    _: str = Depends(require_user),
    namespace: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    pods = await _k8s_pods(namespace)

    # 用量来自 cAdvisor，按 (namespace, pod) 对齐；无样本的 pod 用量为 None
    usage_cpu: dict[tuple[str, str], float] = {}
    usage_mem: dict[tuple[str, str], float] = {}
    try:
        for s in await clients.prometheus.query(_POD_CPU):
            usage_cpu[(s.metric.get("namespace", ""), s.metric.get("pod", ""))] = s.value
        for s in await clients.prometheus.query(_POD_MEM):
            usage_mem[(s.metric.get("namespace", ""), s.metric.get("pod", ""))] = s.value
    except Exception:  # noqa: BLE001  软依赖
        pass

    items = []
    for p in sorted(pods, key=lambda x: (x.namespace, x.name)):
        key = (p.namespace, p.name)
        items.append(
            {
                "namespace": p.namespace,
                "name": p.name,
                "workload": p.workload,
                "node": p.node,
                "status": p.status,
                "ready": p.ready,
                "restarts": p.restarts,
                "age_seconds": p.age_seconds,
                "cpu_usage_cores": usage_cpu.get(key),
                "memory_usage_bytes": usage_mem.get(key),
                "memory_limit_bytes": p.memory_limit_bytes,
            }
        )
    return _paginate(items, page, page_size)


@router.get("/nodes", summary="节点列表（§3.4）")
async def list_nodes(
    _: str = Depends(require_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        nodes = await clients.k8s.list_nodes()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"K8s API 不可达: {e}") from e

    # 单节点场景 aggregate 用量即节点用量；多节点时需按 node label 拆分（TODO）
    cpu_ratio = await _scalar(_CPU_RATIO)
    mem_ratio = await _scalar(_MEM_RATIO)
    items = [
        {
            "name": n.name,
            "status": n.status,
            "roles": n.roles,
            "version": n.version,
            "cpu_alloc_cores": n.cpu_alloc_cores,
            "memory_alloc_bytes": n.memory_alloc_bytes,
            "cpu_usage_ratio": cpu_ratio,
            "memory_usage_ratio": mem_ratio,
            "pods_count": n.pods_count,
        }
        for n in nodes
    ]
    return _paginate(items, page, page_size)


@router.get("/deployments", summary="Deployment 列表（§3.4）")
async def list_deployments(
    _: str = Depends(require_user),
    namespace: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        deps = await clients.k8s.list_deployments(namespace)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"K8s API 不可达: {e}") from e
    deps.sort(key=lambda d: (d.namespace, d.name))
    items = [
        {
            "namespace": d.namespace,
            "name": d.name,
            "replicas": d.replicas,
            "ready_replicas": d.ready_replicas,
            "image": d.image,
            "cpu_limit": d.cpu_limit,
            "memory_limit": d.memory_limit,
        }
        for d in deps
    ]
    return _paginate(items, page, page_size)


@router.get("/events", summary="K8s 事件（§3.4，默认 Warning）")
async def list_events(
    _: str = Depends(require_user),
    type: str = "Warning",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        evs = await clients.k8s.list_events()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"K8s API 不可达: {e}") from e
    if type.lower() != "all":
        evs = [e for e in evs if e.type.lower() == type.lower()]
    evs.sort(key=lambda e: e.last_seen or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    items = [
        {
            "namespace": e.namespace,
            "type": e.type,
            "reason": e.reason,
            "object": e.object,
            "message": e.message,
            "count": e.count,
            "last_seen": e.last_seen.isoformat() if e.last_seen else None,
        }
        for e in evs
    ]
    return _paginate(items, page, page_size)


@router.get("/trends", summary="Dashboard 趋势数据（§3.2.1）")
async def trends(
    _: str = Depends(require_user),
    minutes: int = Query(30, ge=5, le=1440),
):
    step = max(60, minutes * 60 // 240)
    end = datetime.now(timezone.utc).timestamp()
    start = end - minutes * 60

    async def series(expr: str) -> dict[float, float]:
        try:
            sers = await clients.prometheus.query_range(expr, start, end, step)
        except Exception:  # noqa: BLE001  软依赖
            return {}
        merged: dict[float, float] = {}
        for s in sers:
            merged.update(dict(s.values))
        return merged

    qps = await series(_QPS)
    p95 = await series(_P95)
    err_num = await series(_ERROR_RATE_5XX)
    err_den = await series(_ERROR_RATE_TOTAL)
    # 错误率：以分母时间戳为准；无 5xx 序列的时间点视为 0（§3.2.1 与 _error_rate 语义一致）
    err = {
        ts: (err_num.get(ts, 0.0) or 0.0) / max(v, 1e-9)
        for ts, v in err_den.items()
    }
    stamps = sorted(set(qps) | set(err) | set(p95))
    points = [
        {
            "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "qps": qps.get(ts),
            "error_rate": err.get(ts),
            "p95_latency": p95.get(ts),
        }
        for ts in stamps
    ]
    return {"interval_seconds": step, "points": points}
