"""fault-lab 入口：注入编排（状态机/单实验互斥/后台任务）与收尾还原。

inject 流程（phase3-plan §2.1/§2.6，design.md §3.10）：
  created →(inject) injecting →(注入器成功) injected →(评估器收敛) finished
  注入失败回 created（可重试，错误存 params._last_error）。
路由层调 spawn_inject() 后台执行（202 即返），inject_experiment 内部：
  互斥检查（DB 状态 + 进程内锁原子化）→ 状态置 injecting → 三策略注入器 →
  记 injected_at → 启动评估监听（pod_crash 另启循环打崩任务）。

单实验互斥（§2.6）：DB 状态检查（status IN injecting/injected）+ asyncio.Lock
串行化 check-and-set。MVP 单进程部署，不引入 Redis 锁——实验互斥状态本身
就在 DB，重启后由 recover_experiments 收敛。
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from core.db import SessionLocal
from faultlab import evaluator, injectors
from models import Experiment
from monitoring import clients
from risk_control.audit import write_audit

logger = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()  # 持引用防 GC（asyncio.create_task 陷阱）
_inject_lock = asyncio.Lock()

_CREATED = "created"
_INJECTING = "injecting"
_INJECTED = "injected"
_RUNNING = (_INJECTING, _INJECTED)


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------- 注入编排 ----------


def spawn_inject(exp_id: int) -> None:
    """路由层入口：后台执行注入流程（POST /inject 202 即返）。"""
    _spawn(_inject_flow(exp_id))


async def _inject_flow(exp_id: int) -> None:
    try:
        await inject_experiment(exp_id)
    except injectors.InjectorError as e:
        await _back_to_created(exp_id, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("注入流程异常 experiment=%s", exp_id)
        await _back_to_created(exp_id, f"{type(e).__name__}: {e}"[:300])


async def inject_experiment(exp_id: int) -> None:
    """注入状态机（调用方负责已在后台任务中）。"""
    async with _inject_lock:
        async with SessionLocal() as db:
            exp = await db.get(Experiment, exp_id)
            if exp is None:
                raise LookupError(f"实验 {exp_id} 不存在")
            if exp.status != _CREATED:
                raise ValueError(f"实验当前状态 {exp.status} 不允许注入")
            other = await running_experiment(db, exclude_id=exp_id)
            if other is not None:
                raise ValueError(f"已有进行中的实验 #{other.id}（同一时刻只允许一个）")
            exp.status = _INJECTING
            await db.commit()

    injector = injectors.INJECTORS.get(exp.fault_type)
    if injector is None:  # 理论不可达（路由层已限 3 类），防御兜底
        raise injectors.InjectorError(f"故障类型 {exp.fault_type} 暂不支持注入")
    extra = await injector(exp)  # 失败由 _inject_flow 统一回 created

    async with SessionLocal() as db:
        exp = await db.get(Experiment, exp_id)
        now = _utcnow()
        exp.params = {**(exp.params or {}), **extra}
        exp.injected_at = now
        exp.status = _INJECTED
        await db.commit()

    _spawn(evaluator.listen(exp_id, now))
    if exp.fault_type == "pod_crash":
        _spawn(injectors.crash_loop(exp_id))
    logger.info("实验 %s 注入成功（%s），评估监听已启动", exp_id, exp.fault_type)


async def _back_to_created(exp_id: int, message: str) -> None:
    """注入失败回 created（可重试），错误存 params._last_error 供 UI 展示。"""
    async with SessionLocal() as db:
        exp = await db.get(Experiment, exp_id)
        if exp is None or exp.status != _INJECTING:
            return
        exp.status = _CREATED
        exp.params = {**(exp.params or {}), "_last_error": message}
        await db.commit()
    async with SessionLocal() as db:
        await write_audit(
            db,
            actor="system",
            action="fault_inject",
            resource=f"experiment/{exp_id}",
            result="failure",
            detail={"error": message},
        )
    logger.warning("实验 %s 注入失败回 created：%s", exp_id, message)


async def running_experiment(db, *, exclude_id: int | None = None) -> Experiment | None:
    """进行中的实验（injecting/injected；单实验互斥依据，phase3-plan §2.6）。"""
    stmt = select(Experiment)
    rows = list((await db.execute(stmt)).scalars().all())
    running = [
        r for r in rows if r.status in _RUNNING and (exclude_id is None or r.id != exclude_id)
    ]
    return running[0] if running else None


# ---------- 收尾还原 ----------


async def restore_experiment(exp_id: int) -> None:
    """收尾还原（§2.2）：oom 还原 limit 快照、cpu_overload 删压力 Pod。

    幂等（评估收敛与启动恢复可能都调用）；失败上抛，由调用方记日志。
    """
    async with SessionLocal() as db:
        exp = await db.get(Experiment, exp_id)
        if exp is None:
            return
        params = dict(exp.params or {})
        fault_type, ns, target = exp.fault_type, exp.target_ns, exp.target_workload

    if fault_type == "oom":
        snapshot = params.get(injectors.SNAPSHOT_KEY) or {}
        container = snapshot.get("container")
        memory_limit = snapshot.get("memory_limit")
        if not (container and memory_limit):
            return  # 注入未落到 patch（无快照）→ 无需还原
        resources: dict = {"limits": {"memory": memory_limit}}
        memory_request = snapshot.get("memory_request")
        if memory_request:
            # 注入时连 requests 一起调低了（K8s 要求 requests ≤ limits），还原同步
            resources["requests"] = {"memory": memory_request}
        body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": container, "resources": resources}]
                    }
                }
            }
        }
        await clients.k8s.patch_deployment(ns, target, body)
        await _audit_restore(
            f"deployment/{ns}/{target}", {"memory_limit": memory_limit}, {"stage": "restore"}
        )
        logger.info("实验 %s 还原 %s/%s memory_limit=%s", exp_id, ns, target, memory_limit)
    elif fault_type == "cpu_overload":
        pod_name = params.get(injectors.STRESS_POD_KEY)
        if not pod_name:
            return
        await clients.k8s.delete_namespaced_pod(ns, pod_name)  # 不存在视为已清理
        await _audit_restore(f"pod/{ns}/{pod_name}", {}, {"stage": "restore"})
        logger.info("实验 %s 删除压力 Pod %s/%s", exp_id, ns, pod_name)
    # pod_crash：循环打崩随事件关联/上限自停，无需还原


async def _audit_restore(resource: str, params: dict, detail: dict) -> None:
    async with SessionLocal() as db:
        await write_audit(
            db,
            actor="system",
            action="fault_restore",
            resource=resource,
            result="success",
            params=params,
            detail=detail,
        )


# ---------- 启动恢复 ----------


async def recover_experiments() -> int:
    """启动恢复（phase3-plan §2.5）：injected → 恢复评估监听（pod_crash 另启
    循环打崩，窗口按原 injected_at 重算）；injecting 遗留 → 有快照先还原，
    再回 created（注入失败可重试）。返回恢复监听的实验数。"""
    async with SessionLocal() as db:
        stmt_injected = select(Experiment).filter_by(status=_INJECTED)
        injected = list((await db.execute(stmt_injected)).scalars().all())
        stmt_injecting = select(Experiment).filter_by(status=_INJECTING)
        stale = list((await db.execute(stmt_injecting)).scalars().all())

    for exp in injected:
        if exp.injected_at is None:
            await _back_to_created(exp.id, "启动恢复：injected_at 缺失，已回 created")
            continue
        _spawn(evaluator.listen(exp.id, exp.injected_at))
        if exp.fault_type == "pod_crash":
            _spawn(injectors.crash_loop(exp.id))
        logger.warning("启动恢复：实验 %s（%s）恢复评估监听", exp.id, exp.fault_type)

    for exp in stale:
        try:
            await restore_experiment(exp.id)  # 注入半途产物尽力清理（best-effort）
        except Exception:  # noqa: BLE001
            logger.exception("启动恢复：实验 %s 半途产物清理失败", exp.id)
        async with SessionLocal() as db:
            row = await db.get(Experiment, exp.id)
            if row is not None and row.status == _INJECTING:
                row.status = _CREATED
                row.params = {**(row.params or {}), "_last_error": "后端重启中断注入，已回 created 可重试"}
                await db.commit()
        logger.warning("启动恢复：实验 %s 注入中断，已回 created", exp.id)

    return len(injected)
