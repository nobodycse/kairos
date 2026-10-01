"""故障注入器三策略（architecture.md §10 注入方式表）。

全部只作用于 demo namespace（§10 引言）。注入器只负责"把故障放进集群"并
尽量自清理半途产物；事件关联、评估与实验收尾在 evaluator.py。

- oom：patch 目标 Deployment memory_limit → params.memory_limit（默认 128Mi）。
  改模板触发滚动更新，新副本以低 limit 启动 → OOMKilled → PodOOMKilled 规则。
  原值快照交由调用方存 experiment.params._snapshot，实验结束自动还原
  （Agent 若提案调回，128Mi→512Mi=4x 恰好在白名单 0.5x–4x 内）。
- pod_crash：取目标 workload 的一个 Pod IP，POST /internal/crash 打崩一次
  （demo-app 收到请求即 os._exit(1)，响应不会返回——连接被重置即送达）；
  crash_loop 后台继续循环打崩直到重启增量 ≥4（PodCrashLooping 规则
  increase(restarts[10m])>3）且事件已被关联，或触及 8 分钟上限。
- cpu_overload：创建独立 stress-ng 压力 Pod（bare pod，事件 workload 为空，
  靠"同一时刻只允许一个进行中实验"兜底归属）；limits cpu=200m 内打满 →
  ContainerCPUHigh（rate/limit>0.9 for 5m）触发；实验结束删除该 Pod。

安全注记：pod_crash 的请求目标是由 K8s API 查得的集群内 Pod IP（私有段），
属注入机制本体而非用户输入 URL——端口/路径固定，IP 经 ipaddress 校验且必须
属于实验声明目标 workload 的 Pod；本模块不接受任何用户提供的 URL。
"""
import asyncio
import ipaddress
import logging
import time

import httpx

from core.db import SessionLocal
from monitoring import clients
from monitoring.models import PodInfo
from risk_control.audit import resource_for, write_audit

logger = logging.getLogger(__name__)

# params JSONB 的内部键（下划线前缀，report/列表输出时剥离）
SNAPSHOT_KEY = "_snapshot"  # oom：{container, memory_limit, cpu_limit}
STRESS_POD_KEY = "_stress_pod"  # cpu_overload：压力 Pod 名
CRASH_POD_KEY = "_crash_pod"  # pod_crash：被打崩的 Pod 名
CRASH_IP_KEY = "_crash_ip"  # pod_crash：Pod IP
CRASH_BASELINE_KEY = "_crash_baseline_restarts"  # pod_crash：注入前重启计数

OOM_DEFAULT_MEMORY_LIMIT = "128Mi"
STRESS_IMAGE = "polinux/stress-ng"
STRESS_CPU_ARGS = ["--cpu", "2", "--timeout", "900s"]  # 压 15 分钟自止
STRESS_CPU_LIMIT = "200m"  # ContainerCPUHigh 阈值 0.9：2 核压满 / 0.2 核 ≈ 10
CRASH_PORT = 8000
CRASH_PATH = "/internal/crash"
CRASH_TARGET_RESTARTS = 4  # increase(restarts[10m])>3 → 至少 4 次
CRASH_CAP_SECONDS = 480  # 循环打崩整体上限 8 分钟（phase3-plan §2.2）
STRESS_READY_TIMEOUT_S = 120  # 压力 Pod 启动（镜像拉取）等待上限


class InjectorError(RuntimeError):
    """注入失败（后台任务捕获后实验回 created 可重试，错误存 params._last_error）。"""


def _now() -> float:
    return time.monotonic()


def _validate_pod_ip(ip: str | None) -> str:
    """仅允许集群内单播地址：拒绝环回/组播/链路本地/保留段与未分配。"""
    if not ip:
        raise InjectorError("目标 Pod 尚未分配 IP（可能未 Ready），稍后可重试")
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        raise InjectorError(f"Pod IP 非法：{ip}")
    if (
        addr.is_loopback
        or addr.is_multicast
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_unspecified
    ):
        raise InjectorError(f"Pod IP 不可用：{ip}")
    return ip


async def _target_pod(ns: str, workload: str) -> PodInfo:
    """取目标 workload 的一个 Pod（优先 Running Ready；crash 打崩对象）。"""
    pods = await clients.k8s.list_pods(ns)
    candidates = [p for p in pods if p.workload == workload]
    if not candidates:
        raise InjectorError(f"{ns}/{workload} 下没有 Pod")
    healthy = [p for p in candidates if p.phase == "Running" and p.ready]
    return healthy[0] if healthy else candidates[0]


async def _crash_once(ip: str) -> None:
    """POST /internal/crash。进程即死、响应不返回：HTTPError（连接重置/超时）
    一律吞掉，是否真的打崩由下一轮重启计数判定。"""
    url = f"http://{ip}:{CRASH_PORT}{CRASH_PATH}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url)
    except httpx.HTTPError:
        pass


async def _audit_inject(resource: str, params: dict, detail: dict) -> None:
    async with SessionLocal() as db:
        await write_audit(
            db,
            actor="system",
            action="fault_inject",
            resource=resource,
            result="success",
            params=params,
            detail=detail,
        )


# ---------- 三策略 ----------


async def inject_oom(exp) -> dict:
    """调低目标 Deployment memory_limit 触发 OOM（§10 第一行）。"""
    ns, target = exp.target_ns, exp.target_workload
    dep = await clients.k8s.get_deployment(ns, target)
    if dep is None:
        raise InjectorError(f"目标 {ns}/{target} 不存在")
    if not dep.container:
        raise InjectorError(f"目标 {ns}/{target} 无容器，无法调整 limit")
    new_limit = (exp.params or {}).get("memory_limit") or OOM_DEFAULT_MEMORY_LIMIT
    body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": dep.container,
                            "resources": {"limits": {"memory": new_limit}},
                        }
                    ]
                }
            }
        }
    }
    await clients.k8s.patch_deployment(ns, target, body)
    snapshot = {
        "container": dep.container,
        "memory_limit": dep.memory_limit,
        "cpu_limit": dep.cpu_limit,
    }
    await _audit_inject(
        resource_for("deployment", ns, target),
        {"fault_type": "oom", "memory_limit": new_limit},
        {"stage": "inject", "snapshot": snapshot},
    )
    return {SNAPSHOT_KEY: snapshot}


async def inject_pod_crash(exp) -> dict:
    """打崩目标 workload 的一个 Pod 一次；循环打崩由 crash_loop 后台接管。"""
    ns, target = exp.target_ns, exp.target_workload
    pod = await _target_pod(ns, target)
    ip = _validate_pod_ip(pod.pod_ip)
    await _crash_once(ip)
    await _audit_inject(
        resource_for("pod", ns, pod.name),
        {"fault_type": "pod_crash", "workload": target},
        {"stage": "inject", "pod_ip": ip},
    )
    return {
        CRASH_POD_KEY: pod.name,
        CRASH_IP_KEY: ip,
        CRASH_BASELINE_KEY: pod.restarts,
    }


async def inject_cpu_overload(exp) -> dict:
    """创建 stress-ng 压力 Pod 并等到 Running（§10 CPU 过载行）。"""
    ns = exp.target_ns
    pod_name = f"stress-cpu-{exp.id}"
    body = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": ns,
            "labels": {"app": pod_name, "kairos.io/experiment": str(exp.id)},
        },
        "spec": {
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "stress",
                    "image": STRESS_IMAGE,
                    "args": list(STRESS_CPU_ARGS),
                    "resources": {
                        "requests": {"cpu": STRESS_CPU_LIMIT},
                        "limits": {"cpu": STRESS_CPU_LIMIT, "memory": "128Mi"},
                    },
                }
            ],
        },
    }
    await clients.k8s.create_namespaced_pod(ns, body)
    try:
        await _wait_stress_ready(ns, pod_name)
    except InjectorError:
        # 半途产物自清理：起不来（多为镜像拉取失败）就删掉再报错，实验可重试
        try:
            await clients.k8s.delete_namespaced_pod(ns, pod_name)
        except Exception:  # noqa: BLE001  清理失败不掩盖原始错误
            logger.exception("压力 Pod 清理失败 %s/%s", ns, pod_name)
        raise
    await _audit_inject(
        resource_for("pod", ns, pod_name),
        {"fault_type": "cpu_overload", "image": STRESS_IMAGE},
        {"stage": "inject", "pod": pod_name},
    )
    return {STRESS_POD_KEY: pod_name}


async def _wait_stress_ready(ns: str, pod_name: str) -> None:
    """等压力 Pod Running；ErrImagePull 等直接判失败（phase3-plan §7 预案）。"""
    deadline = _now() + STRESS_READY_TIMEOUT_S
    while _now() < deadline:
        pod = await clients.k8s.get_pod(ns, pod_name)
        if pod is None:
            raise InjectorError(f"压力 Pod {ns}/{pod_name} 创建后消失")
        if pod.phase == "Running":
            return
        if pod.status in ("ErrImagePull", "ImagePullBackOff", "InvalidImageName"):
            raise InjectorError(f"压力镜像拉取失败（{pod.status}）：可先在节点预热 {STRESS_IMAGE}")
        await asyncio.sleep(3)
    raise InjectorError(f"压力 Pod {ns}/{pod_name} 在 {STRESS_READY_TIMEOUT_S}s 内未 Running")


# ---------- pod_crash 循环打崩 ----------


async def crash_loop(exp_id: int) -> None:
    """循环打崩（phase3-plan §2.2）：重启增量 ≥4 且事件已关联 → 停；Pod 消失
    或 8 分钟上限收敛。事件关联后即停手：继续打崩会干扰 Agent 修复与验证
    （restart_deployment 替换 Pod 后旧 IP 自然不可达，也构成隐式终止条件）。"""
    from faultlab import evaluator  # 局部导入避免循环依赖
    from models import Experiment

    async with SessionLocal() as db:
        exp = await db.get(Experiment, exp_id)
        if exp is None:
            return
        ns = exp.target_ns
        params = dict(exp.params or {})
    pod_name = params.get(CRASH_POD_KEY)
    ip = params.get(CRASH_IP_KEY)
    baseline = int(params.get(CRASH_BASELINE_KEY) or 0)
    if not pod_name or not ip:
        logger.warning("crash_loop：实验 %s 缺少打崩参数，跳过", exp_id)
        return

    deadline = _now() + CRASH_CAP_SECONDS
    cycles = 0
    try:
        while _now() < deadline:
            associated = await evaluator.event_associated(exp_id)
            if cycles >= CRASH_TARGET_RESTARTS and associated:
                return
            pod = await clients.k8s.get_pod(ns, pod_name)
            if pod is None:
                return  # 被修复动作替换/删除 → 终止
            if pod.restarts > baseline:
                baseline = pod.restarts
                cycles += 1
            # 只在容器 Ready 时打崩：CrashLoopBackOff 等待期 POST 不会命中
            if pod.ready and pod.pod_ip == ip and (
                cycles < CRASH_TARGET_RESTARTS or not associated
            ):
                await _crash_once(ip)
            await asyncio.sleep(5)
    except Exception:  # noqa: BLE001  循环打崩失败不影响实验评估主链路
        logger.exception("crash_loop 异常 experiment=%s", exp_id)
    finally:
        logger.info("crash_loop 结束 experiment=%s cycles=%s", exp_id, cycles)


INJECTORS = {
    "oom": inject_oom,
    "pod_crash": inject_pod_crash,
    "cpu_overload": inject_cpu_overload,
}
