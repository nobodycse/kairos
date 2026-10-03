"""Agent 工具层（architecture.md §7.2）。

8 个查询工具注册给 LLM function calling；5 个修复工具只有参数 schema
（REMEDIATION_PARAM_MODELS），**不进 LLM 工具列表**——LLM 不持有写权限，
修复动作只能由 propose_fix 结构化提议、经 risk_gate/executor 执行。
每个查询工具返回 Evidence（§6.4），data 截断：日志 200 行、指标 60 点（§7.3）。

工具参数名与 mock.py 的 SSE agent_step 契约对齐（get_pod_status 用 name、
get_k8s_events/get_pod_logs 用 pod）。目标不存在返回 exists=False 的 Evidence
而非报错——"pod 没了"本身是诊断信息；基础设施异常抛 ToolError 由调用方处理。
"""
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field

from agent.schemas import Evidence
from monitoring import clients

MAX_LOG_LINES = 200
MAX_METRIC_POINTS = 60
DEFAULT_STEP_SECONDS = 30  # 30 分钟窗口 + 30s 步长 = 60 点
MAX_EVENTS = 50  # 事件条数安全上限（文档未规定，防异常环境刷爆上下文）


class ToolError(RuntimeError):
    """工具执行失败——由调用方（阶段二 collect 循环）决定如何转述给 LLM。"""


# ---------- 参数 schema（pydantic schema 同时生成 OpenAI function 参数） ----------


class GetPodStatusArgs(BaseModel):
    namespace: str = Field(description="K8s 命名空间")
    name: str = Field(description="Pod 名称")


class DescribePodArgs(BaseModel):
    namespace: str = Field(description="K8s 命名空间")
    name: str = Field(description="Pod 名称")


class GetPodLogsArgs(BaseModel):
    namespace: str = Field(description="K8s 命名空间")
    pod: str = Field(description="Pod 名称")
    minutes: int = Field(default=15, ge=1, le=1440, description="查询最近多少分钟的日志")
    filter: str | None = Field(default=None, description="行过滤关键词（如 ERROR / Traceback）")


class GetK8sEventsArgs(BaseModel):
    namespace: str = Field(description="K8s 命名空间")
    pod: str | None = Field(default=None, description="只看这个 Pod 的事件（不填看整个命名空间）")
    minutes: int = Field(default=30, ge=1, le=1440, description="查询最近多少分钟的事件")


class GetMetricsArgs(BaseModel):
    namespace: str = Field(description="K8s 命名空间")
    target: str = Field(description="Pod 名或 workload 名（前缀匹配 Pod）")
    metric: Literal["cpu_usage", "memory_usage", "restarts", "error_rate", "p95_latency"] = Field(
        description="指标：cpu_usage(核)/memory_usage(字节)/restarts(次数)/error_rate(0-1)/p95_latency(秒)"
    )
    minutes: int = Field(default=30, ge=1, le=1440, description="查询最近多少分钟")


class GetDeploymentArgs(BaseModel):
    namespace: str = Field(description="K8s 命名空间")
    name: str = Field(description="Deployment 名称")


class GetNodeStatusArgs(BaseModel):
    node: str | None = Field(default=None, description="节点名（不填返回全部节点）")


class GetServiceStatusArgs(BaseModel):
    namespace: str = Field(description="K8s 命名空间")
    service: str = Field(description="Service 名称")


# PromQL 模板与 deploy/observability/prometheus/rules.yml 的告警口径一致
_POD_EXPRS = {
    "cpu_usage": 'rate(container_cpu_usage_seconds_total{{namespace="{ns}",pod=~"{pod}.*",container!=""}}[5m])',
    "memory_usage": 'container_memory_working_set_bytes{{namespace="{ns}",pod=~"{pod}.*",container!=""}}',
    "restarts": 'increase(kube_pod_container_status_restarts_total{{namespace="{ns}",pod=~"{pod}.*"}}[10m])',
}
# demo_http_* 只有 status/route 标签（无 pod），error_rate/p95_latency 是全局口径
_GLOBAL_EXPRS = {
    "error_rate": "sum(rate(demo_http_requests_total{status=~\"5..\"}[5m])) / sum(rate(demo_http_requests_total[5m]))",
    "p95_latency": "histogram_quantile(0.95, sum by (le)(rate(demo_http_request_duration_seconds_bucket[5m])))",
}


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    args_schema: type[BaseModel]
    handler: Callable[[BaseModel], Awaitable[Evidence]]


# ---------- 8 个查询工具 handler ----------


def _missing(tool: str, kind: str, ident: str, extra: dict[str, Any] | None = None) -> Evidence:
    data: dict[str, Any] = {"exists": False, "kind": kind, "name": ident}
    if extra:
        data.update(extra)
    return Evidence(source="k8s_api", tool=tool, summary=f"{kind} {ident} 不存在（可能已被重建或删除）", data=data)


async def _get_pod_status(args: GetPodStatusArgs) -> Evidence:
    pod = await clients.k8s.get_pod(args.namespace, args.name)
    if pod is None:
        return _missing("get_pod_status", "Pod", f"{args.namespace}/{args.name}")
    return Evidence(
        source="k8s_api",
        tool="get_pod_status",
        summary=f"Pod {args.name}：{pod.status}，ready={str(pod.ready).lower()}，重启 {pod.restarts} 次，节点 {pod.node}",
        data=pod.model_dump(),
    )


async def _describe_pod(args: DescribePodArgs) -> Evidence:
    desc = await clients.k8s.describe_pod(args.namespace, args.name)
    if desc is None:
        return _missing("describe_pod", "Pod", f"{args.namespace}/{args.name}")
    containers = desc.get("containers") or []
    reasons = [c["state"].get("waiting", {}).get("reason") or c["state"].get("terminated", {}).get("reason")
               for c in containers if c.get("state")]
    reason_txt = "，".join(r for r in reasons if r) or "无异常状态"
    return Evidence(
        source="k8s_api",
        tool="describe_pod",
        summary=f"Pod {args.name}（{desc.get('phase')}）：{reason_txt}，容器 {len(containers)} 个",
        data=desc,
    )


async def _get_pod_logs(args: GetPodLogsArgs) -> Evidence:
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=args.minutes)
    logql = f'{{namespace="{args.namespace}", pod="{args.pod}"}}'
    if args.filter:
        logql += f' |= "{args.filter}"'
    # 多查 1 行用于判断是否被截断
    lines = await clients.loki.query_range(logql, start, end, limit=MAX_LOG_LINES + 1)
    kept = [line.line for line in lines[:MAX_LOG_LINES]]
    return Evidence(
        source="loki",
        tool="get_pod_logs",
        summary=f"最近 {args.minutes} 分钟日志 {len(kept)} 行"
        + (f"（过滤 {args.filter!r}）" if args.filter else ""),
        data={
            "pod": args.pod,
            "filter": args.filter,
            "window_minutes": args.minutes,
            "lines_returned": len(lines),
            "lines": kept,
            "truncated": len(lines) > MAX_LOG_LINES,
        },
    )


async def _get_k8s_events(args: GetK8sEventsArgs) -> Evidence:
    events = await clients.k8s.list_events(args.namespace)
    if args.pod:
        key = f"{args.namespace}/{args.pod}"
        events = [e for e in events if e.object == key]
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=args.minutes)
    events = [e for e in events if e.last_seen is None or e.last_seen >= cutoff]
    events = events[:MAX_EVENTS]
    warnings = [e for e in events if e.type == "Warning"]
    top = Counter(e.reason for e in warnings if e.reason).most_common(3)
    top_txt = "，".join(f"{reason}×{count}" for reason, count in top) or "无 Warning"
    return Evidence(
        source="events",
        tool="get_k8s_events",
        summary=f"最近 {args.minutes} 分钟 {len(events)} 条事件，Warning {len(warnings)} 条（{top_txt}）",
        data={
            "pod": args.pod,
            "window_minutes": args.minutes,
            "count": len(events),
            "warning_count": len(warnings),
            "events": [e.model_dump(mode="json") for e in events],
        },
    )


async def _get_metrics(args: GetMetricsArgs) -> Evidence:
    now = time.time()
    step = max(DEFAULT_STEP_SECONDS, args.minutes * 60 // MAX_METRIC_POINTS)
    if args.metric in _POD_EXPRS:
        # PromQL 用 RE2：不认 Python re.escape 的 `\-` 转义，还原后再拼
        pod = re.escape(args.target).replace("\\-", "-")
        expr = _POD_EXPRS[args.metric].format(ns=args.namespace, pod=pod)
    else:
        expr = _GLOBAL_EXPRS[args.metric]
    series = await clients.prometheus.query_range(expr, now - args.minutes * 60, now, step)
    out_series: list[dict[str, Any]] = []
    total_points = 0
    truncated = False
    for s in series:
        points = s.values[-MAX_METRIC_POINTS:]
        truncated = truncated or len(s.values) > MAX_METRIC_POINTS
        total_points += len(points)
        out_series.append(
            {"metric": s.metric, "points": [[int(ts), round(v, 6)] for ts, v in points]}
        )
    last_val = out_series[0]["points"][-1][1] if out_series and out_series[0]["points"] else None
    summary = f"{args.metric}（{args.target}，{args.minutes} 分钟，step {step}s）：{len(out_series)} 条序列"
    if last_val is not None:
        summary += f"，最新值 {last_val}"
    return Evidence(
        source="prometheus",
        tool="get_metrics",
        summary=summary,
        data={
            "metric": args.metric,
            "target": args.target,
            "window_minutes": args.minutes,
            "step_seconds": step,
            "series_count": len(out_series),
            "points": total_points,
            "series": out_series,
            "truncated": truncated,
        },
    )


async def _get_deployment(args: GetDeploymentArgs) -> Evidence:
    dep = await clients.k8s.get_deployment(args.namespace, args.name)
    if dep is None:
        return _missing("get_deployment", "Deployment", f"{args.namespace}/{args.name}")
    return Evidence(
        source="k8s_api",
        tool="get_deployment",
        summary=f"Deployment {args.name}：{dep.ready_replicas}/{dep.replicas} 副本就绪，镜像 {dep.image}，"
        f"cpu_limit={dep.cpu_limit} memory_limit={dep.memory_limit}",
        data=dep.model_dump(),
    )


async def _get_node_status(args: GetNodeStatusArgs) -> Evidence:
    if args.node:
        node = await clients.k8s.get_node(args.node)
        nodes = [node] if node else []
    else:
        nodes = await clients.k8s.list_nodes()
    if not nodes:
        return _missing("get_node_status", "Node", args.node or "(all)")
    not_ready = [n.name for n in nodes if n.status != "Ready"]
    summary = f"{len(nodes)} 个节点，{'全部 Ready' if not not_ready else 'NotReady：' + '、'.join(not_ready)}"
    return Evidence(
        source="k8s_api",
        tool="get_node_status",
        summary=summary,
        data={"count": len(nodes), "nodes": [n.model_dump() for n in nodes]},
    )


async def _get_service_status(args: GetServiceStatusArgs) -> Evidence:
    eps = await clients.k8s.get_service_endpoints(args.namespace, args.service)
    qps: float | None = None
    error_rate: float | None = None
    try:
        samples = await clients.prometheus.query("sum(rate(demo_http_requests_total[5m]))")
        if samples:
            qps = round(samples[0].value, 4)
        errs = await clients.prometheus.query(_GLOBAL_EXPRS["error_rate"])
        if errs:
            error_rate = round(errs[0].value, 4)
    except Exception:
        pass  # Prometheus 软依赖：拿不到不阻断（与 cluster 路由的软依赖语义一致）
    if eps is None:
        return Evidence(
            source="k8s_api",
            tool="get_service_status",
            summary=f"Service {args.namespace}/{args.service} 不存在或无 Endpoints",
            data={"exists": False, "service": args.service, "qps": qps, "error_rate": error_rate},
        )
    summary = (
        f"Service {args.service}：{eps.ready_addresses} 就绪 / {eps.not_ready_addresses} 未就绪 endpoint"
    )
    if qps is not None:
        summary += f"，QPS {qps}"
    return Evidence(
        source="k8s_api",
        tool="get_service_status",
        summary=summary,
        data={"exists": True, "endpoints": eps.model_dump(), "qps": qps, "error_rate": error_rate},
    )


QUERY_TOOLS: dict[str, ToolDef] = {
    t.name: t
    for t in (
        ToolDef("get_pod_status", "查询单个 Pod 的运行状态（phase/ready/重启次数/节点/内存 limit）", GetPodStatusArgs, _get_pod_status),
        ToolDef("get_pod_logs", "查询 Pod 最近日志（Loki），可按关键词过滤", GetPodLogsArgs, _get_pod_logs),
        ToolDef("get_k8s_events", "查询 K8s 事件（Warning/OOMKilled/CrashLoopBackOff 等）", GetK8sEventsArgs, _get_k8s_events),
        ToolDef("get_metrics", "查询指标曲线（CPU/内存/重启/5xx 率/P95 延迟）", GetMetricsArgs, _get_metrics),
        ToolDef("get_deployment", "查询 Deployment 的副本/镜像/资源 limit", GetDeploymentArgs, _get_deployment),
        ToolDef("get_node_status", "查询节点状态（Ready/资源分配/Pod 数）", GetNodeStatusArgs, _get_node_status),
        ToolDef("describe_pod", "查询 Pod 详情（conditions/各容器状态与退出原因）", DescribePodArgs, _describe_pod),
        ToolDef("get_service_status", "查询 Service 的 Endpoints 与流量（QPS/5xx 率）", GetServiceStatusArgs, _get_service_status),
    )
}


def to_openai_specs() -> list[dict[str, Any]]:
    """生成 chat(messages, tools=...) 的 OpenAI function calling 载荷（§7.3）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.args_schema.model_json_schema(),
            },
        }
        for t in QUERY_TOOLS.values()
    ]


async def call_tool(name: str, arguments: dict[str, Any]) -> Evidence:
    """统一入口：查注册表 → 校验参数 → 执行。异常统一包成 ToolError。"""
    tool = QUERY_TOOLS.get(name)
    if tool is None:
        raise ToolError(f"未知工具：{name}")
    try:
        args = tool.args_schema.model_validate(arguments)
        return await tool.handler(args)
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"{name} 执行失败：{type(e).__name__}: {e}") from e


# ---------- 5 个修复工具的参数 schema（§7.2 工具隔离：不进 LLM 工具列表） ----------
# 只定义参数形态；业务约束（replicas∈[0,10]、limit 0.5x–4x、managed 标签、
# delete_pod 仅限独立 Pod）由 risk_control.whitelist 统一裁决（§11.1 单一权威）。


class UpdateResourceLimitParams(BaseModel):
    container: str = Field(description="容器名")
    cpu_limit: str | None = Field(default=None, description='新 CPU limit（quantity，如 "500m"）')
    memory_limit: str | None = Field(default=None, description='新内存 limit（quantity，如 "1Gi"）')


class ScaleDeploymentParams(BaseModel):
    replicas: int = Field(description="目标副本数")


class RestartDeploymentParams(BaseModel):
    pass


class RollbackDeploymentParams(BaseModel):
    pass


class DeletePodParams(BaseModel):
    """delete_pod 无参数：目标 Pod 名由 RemediationPlan.target 承载。"""

    pass


REMEDIATION_PARAM_MODELS: dict[str, type[BaseModel]] = {
    "update_resource_limit": UpdateResourceLimitParams,
    "scale_deployment": ScaleDeploymentParams,
    "restart_deployment": RestartDeploymentParams,
    "rollback_deployment": RollbackDeploymentParams,
    "delete_pod": DeletePodParams,
}
