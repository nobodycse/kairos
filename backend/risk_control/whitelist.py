"""参数白名单（architecture.md §11.1）：LLM 提议 → 白名单校验 → decide()。

五条规则（任一越界 → 拒绝 + 写审计 result=denied）：
1. namespace ∈ {settings.demo_namespace}（demo）
2. scale_deployment.replicas ∈ [0, 10]
3. update_resource_limit 只允许在当前值的 0.5x–4x 区间内调整
4. 目标 workload 必须存在且带 kairos.io/managed=true 标签
5. delete_pod 只允许删除独立 Pod（ownerReferences 无 Deployment/StatefulSet/
   DaemonSet，即"流氓/压测 Pod"）——受管工作负载的 Pod 一律拒绝：Agent 永远
   不能删业务 Pod

demo 环境 workload 均为单容器，调整幅度对照 DeploymentInfo 首容器的
cpu_limit/memory_limit（§6.5 params 示例口径一致）。
"""
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.schemas import RemediationPlan
from agent.tools import REMEDIATION_PARAM_MODELS, UpdateResourceLimitParams
from core.config import settings
from monitoring import clients
from monitoring.k8s import parse_quantity
from monitoring.models import DeploymentInfo, PodInfo
from risk_control.audit import resource_for, write_audit

_MANAGED_LABEL = "kairos.io/managed"
_LIMIT_RATIO_RANGE = (0.5, 4.0)
_REPLICAS_RANGE = (0, 10)


class WhitelistResult(BaseModel):
    allowed: bool
    violations: list[str]


async def validate(db: AsyncSession, plan: RemediationPlan) -> WhitelistResult:
    """校验修复提议；拒绝时写审计（actor=agent, result=denied）。"""
    violations: list[str] = []

    params = None
    params_model = REMEDIATION_PARAM_MODELS.get(plan.action)
    if params_model is None:
        violations.append(f"未知动作 {plan.action}（不在修复工具清单中）")
    else:
        try:
            params = params_model.model_validate(plan.params)
        except ValidationError as e:
            detail = "; ".join(
                f"{'.'.join(str(loc) for loc in err['loc']) or '(root)'}: {err['msg']}"
                for err in e.errors()[:3]
            )
            violations.append(f"params 不符合 {plan.action} 的 schema：{detail}")

    if plan.namespace != settings.demo_namespace:
        violations.append(
            f"namespace {plan.namespace!r} 越界（白名单仅允许 {settings.demo_namespace!r}）"
        )

    if params is not None and plan.action == "scale_deployment":
        replicas = params.replicas
        if not (_REPLICAS_RANGE[0] <= replicas <= _REPLICAS_RANGE[1]):
            violations.append(
                f"replicas={replicas} 越界（白名单 {_REPLICAS_RANGE}）"
            )

    dep: DeploymentInfo | None = None
    pod: PodInfo | None = None
    if plan.namespace == settings.demo_namespace:
        # namespace 越界时不必查集群；这里 404 → None（目标不存在），基础设施异常向上抛
        if plan.action == "delete_pod":
            pod = await clients.k8s.get_pod(plan.namespace, plan.target)
        else:
            dep = await clients.k8s.get_deployment(plan.namespace, plan.target)

    if plan.action == "delete_pod":
        # delete_pod 的 target 是 Pod 名而非 workload 名，走 bare-pod-only 校验
        violations.extend(_delete_pod_violations(pod))
    elif dep is None:
        violations.append(f"目标 workload {plan.namespace}/{plan.target} 不存在")
    else:
        managed = (dep.labels or {}).get(_MANAGED_LABEL)
        if managed != "true":
            violations.append(
                f"目标缺少 {_MANAGED_LABEL}=true 标签（实际值：{managed!r}）"
            )
        if plan.action == "update_resource_limit" and params is not None:
            violations.extend(_limit_ratio_violations(params, dep))

    result = WhitelistResult(allowed=not violations, violations=violations)
    if not result.allowed:
        await write_audit(
            db,
            actor="agent",
            action=plan.action,
            resource=resource_for(
                "pod" if plan.action == "delete_pod" else "deployment",
                plan.namespace, plan.target,
            ),
            params=plan.params,
            result="denied",
            detail={"violations": violations, "plan_reason": plan.reason},
        )
    return result


def _delete_pod_violations(pod: PodInfo | None) -> list[str]:
    """delete_pod 白名单校验（存在性 + bare-pod-only，规则 5）。

    pod.workload 由 ownerReferences 推导：Deployment（直属/经 RS）与
    StatefulSet/DaemonSet → 名字，无 owner 与 Job → None。workload 非 None
    即受管工作负载，一律拒绝——Agent 永远不能删业务 Pod（Phase 4 核心
    安全边界）；None 视为独立 Pod 放行（demo ns 压测/流氓 Pod 场景，
    Job 属派生 None 的既有口径，demo ns 无 Job 负载）。纯函数便于冒烟打桩。
    """
    if pod is None:
        return ["目标 Pod 不存在（可能已被删除或名字有误）"]
    if pod.workload is not None:
        return [
            f"Pod {pod.namespace}/{pod.name} 属于受管工作负载 {pod.workload}，"
            "不允许删除（Agent 只能删独立 Pod；业务 Pod 请用其它修复动作）"
        ]
    return []


def _limit_ratio_violations(
    params: UpdateResourceLimitParams, dep: DeploymentInfo
) -> list[str]:
    violations: list[str] = []
    if params.memory_limit is None and params.cpu_limit is None:
        violations.append("update_resource_limit 至少要给出 cpu_limit 或 memory_limit 之一")
        return violations
    low, high = _LIMIT_RATIO_RANGE
    for name, new_q, cur_q in (
        ("memory_limit", params.memory_limit, dep.memory_limit),
        ("cpu_limit", params.cpu_limit, dep.cpu_limit),
    ):
        if new_q is None:
            continue
        new_v = parse_quantity(new_q)
        if new_v is None or new_v <= 0:
            violations.append(f"{name} 新值 {new_q!r} 不是合法的 K8s quantity")
            continue
        cur_v = parse_quantity(cur_q)
        if cur_v is None or cur_v <= 0:
            violations.append(f"{name} 当前值未设置（{cur_q!r}），无法校验调整幅度")
            continue
        ratio = new_v / cur_v
        if not (low <= ratio <= high):
            violations.append(
                f"{name} 调整 {cur_q}→{new_q}（{ratio:.2f}x）超出 {low}x–{high}x 区间"
            )
    return violations
