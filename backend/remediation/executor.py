"""修复执行器（architecture.md §6.5/§6.6）：execute / rollback / 快照 / 审计。

顺序约定（phase2-plan §5 风险条）：白名单再校验 → **快照先落库** → patch →
执行后快照 + before/after 两条审计（含字段级 diff）。所有动作经 patch 而非
replace（§6.5 L367）。

rollback_deployment 执行语义（文档未定义，落地决策 §一.1）：按该 workload
最近一次已执行动作（status=succeeded 且 snapshot 非空）的快照恢复——与验证
不过的自动回滚同一机制；无快照可回滚则执行失败。

delete_pod 执行语义（phase4-plan §2.3，破坏性动作）：target 是 Pod 名——
Pod 快照（phase/重启数/owner）先落库 → 删除 Pod（白名单保证只能是独立
Pod）→ 审计。**不可回滚**：Pod 无法复活，rollback() 对该动作短路返回
success=False，验证不过即走既有 failed 路径转人工，不做任何恢复尝试。
"""
import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import RemediationAction
from monitoring import clients
from risk_control import Decision
from risk_control.audit import resource_for, write_audit
from risk_control.whitelist import validate as whitelist_validate

logger = logging.getLogger(__name__)

_RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"  # rollout restart 惯例注解


class ExecResult(BaseModel):
    """执行结果（文档未定义字段，落地决策 §一.3 最小结构）。"""

    success: bool
    message: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _snapshot_of(dep, container: str | None = None) -> dict[str, Any]:
    """执行前快照（§6.5：Deployment 当前资源）。container 供 limits 恢复定位。

    memory_request 一并入快照：回滚只恢复 limits 会与现存 requests 冲突
    （requests ≤ limits 是 K8s 硬约束，fault-lab 的 oom 注入连 requests 一起
    调低后，回滚缺 requests 会 422——服务器实测）。
    """
    return {
        "replicas": dep.replicas,
        "image": dep.image,
        "cpu_limit": dep.cpu_limit,
        "memory_limit": dep.memory_limit,
        "memory_request": dep.memory_request,
        "container": container,
    }


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        k: {"before": before.get(k), "after": after.get(k)}
        for k in before
        if before.get(k) != after.get(k)
    }


def _patch_body(plan) -> dict[str, Any]:
    """三类动作的 patch 体（restart/scale/limits）。rollback_deployment 不在此列。"""
    if plan.action == "restart_deployment":
        return {
            "spec": {
                "template": {
                    "metadata": {"annotations": {_RESTART_ANNOTATION: _now().isoformat()}}
                }
            }
        }
    if plan.action == "scale_deployment":
        return {"spec": {"replicas": int(plan.params["replicas"])}}
    if plan.action == "update_resource_limit":
        # 参数名 → K8s 资源名（limits 的键必须是 cpu/memory）
        resource_keys = {"cpu_limit": "cpu", "memory_limit": "memory"}
        limits = {
            resource_keys[k]: plan.params[k]
            for k in resource_keys
            if plan.params.get(k)
        }
        return {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": plan.params.get("container"),
                                "resources": {"limits": limits},
                            }
                        ]
                    }
                }
            }
        }
    raise ValueError(f"executor.execute 不支持直接执行 {plan.action}")


def _restore_body(snapshot: dict[str, Any]) -> dict[str, Any]:
    """按快照恢复的 patch 体：只恢复快照中存在的字段。

    limits/image 的 strategic merge 需要 container name 作 merge key——快照无
    container 时只恢复 replicas（记日志）。
    """
    spec: dict[str, Any] = {}
    if snapshot.get("replicas") is not None:
        spec["replicas"] = snapshot["replicas"]
    container_patch: dict[str, Any] = {}
    if snapshot.get("image"):
        container_patch["image"] = snapshot["image"]
    limits = {
        k: v
        for k, v in (("cpu", snapshot.get("cpu_limit")), ("memory", snapshot.get("memory_limit")))
        if v
    }
    requests = {"memory": snapshot["memory_request"]} if snapshot.get("memory_request") else {}
    resources: dict[str, Any] = {}
    if limits:
        resources["limits"] = limits
    if requests:
        resources["requests"] = requests
    if resources:
        container_patch["resources"] = resources
    if container_patch:
        if not snapshot.get("container"):
            logger.warning("快照缺 container 名，跳过模板恢复（仅恢复 replicas）：%s", snapshot)
            container_patch = {}
        else:
            container_patch["name"] = snapshot["container"]
            spec["template"] = {"spec": {"containers": [container_patch]}}
    return {"spec": spec}


async def _event_remediations(db: AsyncSession, fault_event_id: int) -> list:
    """该事件的全部 remediation 行（单事件量级极小，细过滤在 Python 侧）。"""
    rows = await db.execute(
        select(RemediationAction).filter_by(fault_event_id=fault_event_id)
    )
    return list(rows.scalars().all())


async def _find_remediation(db: AsyncSession, fault_event_id: int, action: str):
    """该事件该动作最新的待执行提案（approved 优先于 pending，各取最新）。"""
    usable = [
        r
        for r in await _event_remediations(db, fault_event_id)
        if r.action == action and r.status in ("approved", "pending")
    ]
    usable.sort(key=lambda r: (r.status != "approved", r.created_at))
    return usable[-1] if usable else None


async def _latest_succeeded_row(
    db: AsyncSession, fault_event_id: int, namespace: str, target: str
):
    """该 workload 最近一次已执行成功且带快照的 remediation 行（回滚依据）。"""
    cands = [
        r
        for r in await _event_remediations(db, fault_event_id)
        if r.status == "succeeded"
        and r.snapshot
        and r.namespace == namespace
        and r.target == target
    ]
    cands.sort(key=lambda r: r.executed_at or r.created_at)
    return cands[-1] if cands else None


async def execute(
    db: AsyncSession, fault_event_id: int, plan, decision: Decision
) -> ExecResult:
    """执行修复提案（§6.5）：白名单 → 快照落库 → patch/删除 → 审计。"""
    # 1) 白名单再校验（拒绝时自带 denied 审计，§11.1 防线兜底）
    wl = await whitelist_validate(db, plan)
    if not wl.allowed:
        return ExecResult(success=False, message="白名单拒绝：" + "；".join(wl.violations))
    # 2) 提案行
    rem = await _find_remediation(db, fault_event_id, plan.action)
    if rem is None:
        return ExecResult(success=False, message="找不到对应的修复提案记录")
    # delete_pod 走独立分支：target 是 Pod 名，无 patch/回滚语义（phase4-plan §2.3）
    if plan.action == "delete_pod":
        return await _execute_delete_pod(db, plan, rem)
    # 3) 快照先落库（顺序不可颠倒，否则无法恢复）
    dep = await clients.k8s.get_deployment(plan.namespace, plan.target)
    if dep is None:
        return ExecResult(success=False, message=f"目标 {plan.namespace}/{plan.target} 不存在")
    container = plan.params.get("container") if plan.action == "update_resource_limit" else None
    snapshot = _snapshot_of(dep, container=container)
    rem.snapshot = snapshot
    rem.status = "executing"
    await db.commit()
    resource = resource_for("deployment", plan.namespace, plan.target)
    await write_audit(
        db, actor="agent", action=plan.action, resource=resource,
        params=plan.params, result="allowed",
        detail={"stage": "before", "snapshot": snapshot, "reason": plan.reason},
    )
    # 4) patch 执行（rollback_deployment 按快照恢复，落地决策 §一.1）
    try:
        if plan.action == "rollback_deployment":
            source = await _latest_succeeded_row(db, fault_event_id, plan.namespace, plan.target)
            if source is None:
                raise RuntimeError("无可回滚快照（该 workload 没有已执行成功的修复动作）")
            body = _restore_body(source.snapshot)
        else:
            body = _patch_body(plan)
        after_dep = await clients.k8s.patch_deployment(plan.namespace, plan.target, body)
    except Exception as e:
        logger.exception("patch 失败 %s/%s %s", plan.namespace, plan.target, plan.action)
        rem.status = "failed"
        await db.commit()
        await write_audit(
            db, actor="agent", action=plan.action, resource=resource,
            params=plan.params, result="failure",
            detail={"stage": "after", "error": str(e)[:300], "snapshot": snapshot},
        )
        return ExecResult(success=False, message=f"patch 失败：{e}")
    after = _snapshot_of(after_dep)
    rem.status = "succeeded"
    rem.executed_at = _now()
    await db.commit()
    await write_audit(
        db, actor="agent", action=plan.action, resource=resource,
        params=plan.params, result="success",
        detail={"stage": "after", "diff": _diff(snapshot, after), "snapshot": snapshot},
    )
    return ExecResult(success=True, message=f"{plan.action} 执行成功")


async def _execute_delete_pod(db: AsyncSession, plan, rem) -> ExecResult:
    """delete_pod 分支（phase4-plan §2.3）：Pod 快照先落库 → 删除 → 审计。

    快照留 Pod 名/phase/重启数/owner 供事后还原现场（仅审计语义，不用于恢复——
    Pod 删除不可逆，白名单已保证目标只能是独立 Pod）。
    """
    pod = await clients.k8s.get_pod(plan.namespace, plan.target)
    if pod is None:
        return ExecResult(success=False, message=f"目标 Pod {plan.namespace}/{plan.target} 不存在")
    snapshot = {
        "kind": "pod",
        "name": pod.name,
        "phase": pod.phase,
        "status": pod.status,
        "restarts": pod.restarts,
        "workload": pod.workload,  # None = 独立 Pod（白名单已保证放行口径）
        "node": pod.node,
    }
    rem.snapshot = snapshot
    rem.status = "executing"
    await db.commit()
    resource = resource_for("pod", plan.namespace, plan.target)
    await write_audit(
        db, actor="agent", action=plan.action, resource=resource,
        params=plan.params, result="allowed",
        detail={"stage": "before", "snapshot": snapshot, "reason": plan.reason},
    )
    try:
        await clients.k8s.delete_namespaced_pod(plan.namespace, plan.target)
    except Exception as e:
        logger.exception("删除 Pod 失败 %s/%s", plan.namespace, plan.target)
        rem.status = "failed"
        await db.commit()
        await write_audit(
            db, actor="agent", action=plan.action, resource=resource,
            params=plan.params, result="failure",
            detail={"stage": "after", "error": str(e)[:300], "snapshot": snapshot},
        )
        return ExecResult(success=False, message=f"删除 Pod 失败：{e}")
    rem.status = "succeeded"
    rem.executed_at = _now()
    await db.commit()
    await write_audit(
        db, actor="agent", action=plan.action, resource=resource,
        params=plan.params, result="success",
        detail={"stage": "after", "snapshot": snapshot,
                "note": "Pod 已删除（不可逆动作，无恢复语义）"},
    )
    return ExecResult(success=True, message=f"{plan.action} 执行成功：Pod {plan.target} 已删除（不可回滚）")


async def rollback(db: AsyncSession, fault_event_id: int, plan) -> ExecResult:
    """验证不过的自动回滚（§6.5 rollback / §7.1 RB 分支）：按快照恢复。

    delete_pod 不可回滚（Pod 无法复活）：短路返回失败——rollback_node 据此
    置 outcome=failed 转人工，不产生任何集群写操作（对已删除的 Pod 名 patch
    Deployment 既是语义错误也有同名碰撞的理论风险）。
    """
    if plan.action == "delete_pod":
        return ExecResult(success=False, message="delete_pod 不可回滚（Pod 无法复活），转人工处理")
    source = await _latest_succeeded_row(db, fault_event_id, plan.namespace, plan.target)
    if source is None or not source.snapshot:
        return ExecResult(success=False, message="无快照可回滚")
    snapshot = source.snapshot
    resource = resource_for("deployment", plan.namespace, plan.target)
    await write_audit(
        db, actor="agent", action="rollback", resource=resource,
        params=snapshot, result="allowed",
        detail={"stage": "before", "source_remediation_id": source.id},
    )
    try:
        after_dep = await clients.k8s.patch_deployment(
            plan.namespace, plan.target, _restore_body(snapshot)
        )
    except Exception as e:
        logger.exception("回滚 patch 失败 %s/%s", plan.namespace, plan.target)
        await write_audit(
            db, actor="agent", action="rollback", resource=resource,
            params=snapshot, result="failure",
            detail={"stage": "after", "error": str(e)[:300]},
        )
        return ExecResult(success=False, message=f"回滚 patch 失败：{e}")
    after = _snapshot_of(after_dep)
    source.status = "rolled_back"
    await db.commit()
    await write_audit(
        db, actor="agent", action="rollback", resource=resource,
        params=snapshot, result="success",
        detail={"stage": "after", "diff": _diff(snapshot, after), "source_remediation_id": source.id},
    )
    return ExecResult(success=True, message="回滚完成")
