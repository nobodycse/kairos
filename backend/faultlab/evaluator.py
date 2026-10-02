"""实验监听/评估器（architecture.md §8 关联回填 + phase3-plan §2.4 评估口径）。

注入成功后由 inject_experiment 启动 listen()（后台 asyncio 任务，10s 轮询）：

1. 关联窗口（注入后 10 分钟）：回填 demo ns 内未关联的活跃新事件——
   workload==target 或 workload 为空（聚合类告警），detected_at/updated_at
   晚于注入时刻（updated_at 兜底"新告警并入既有活跃事件"的归并场景，
   §5.3 合并不改 detected_at）。未关联 → detected=false 收敛（未检出本身
   是评估结论）。
2. 闭环等待：等事件到终态（resolved/failed/closed）；自 injected_at 起总
   上限 40 分钟（cpu_overload 的 for 5m + 压力 15m 所需），超时按当时事件
   状态快照评估。

评估写入 experiment_results 后实验置 finished，再由 faultlab.
restore_experiment 收尾还原（失败只记日志，不回滚实验状态）。

查询写法沿用 webhooks/executor 的 Mimosa 通过样式：先建 stmt 变量再 execute
（参数化占位符绑定），事件量级小、细过滤在 Python 侧完成。
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from core.db import SessionLocal
from models import (
    ACTIVE_STATUSES,
    AuditLog,
    Diagnosis,
    Experiment,
    ExperimentResult,
    FaultEvent,
    RemediationAction,
)
from risk_control.audit import resource_for

logger = logging.getLogger(__name__)

ASSOC_WINDOW_S = 1200  # 注入后关联窗口：architecture.md §8 原文 10 分钟，但
# ContainerCPUHigh（for 5m + rate[5m] 窗口填充）结构性最早 T+10min 才 firing
# （服务器实测 06:41 注入 06:51:30 firing，差 15s 未关联），放宽到 20 分钟（§2.3 注记）
TOTAL_CAP_S = 2400  # 自注入起的收敛总上限（phase3-plan §2.4）
POLL_INTERVAL_S = 10
_ASSOC_SKEW_S = 30  # 注入时刻与告警时刻的时钟/顺序容差
TERMINAL_STATUSES = ("resolved", "failed", "closed")

# diagnosed_correctly 前缀映射（大小写不敏感，phase3-plan §2.4）。覆盖两类命名：
# RCA 自拟类型（OOM / CrashLoop / CPUThrottling…）与告警同名类型
# （PodOOMKilled / PodCrashLooping / ContainerCPUHigh…，六轮实测 Agent 实际输出）
DIAGNOSIS_PREFIXES = {
    "oom": ("oom", "containeroomkilled", "containermemory"),
    "pod_crash": ("crash", "podcrash", "backoff"),
    "cpu_overload": ("cpu", "containercpu"),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def event_associated(exp_id: int) -> bool:
    """该实验是否已关联到故障事件（injectors.crash_loop 的停手条件）。"""
    experiment_id = exp_id
    async with SessionLocal() as db:
        stmt = select(FaultEvent).filter_by(experiment_id=experiment_id)
        rows = list((await db.execute(stmt)).scalars().all())
        return bool(rows)


async def listen(exp_id: int, injected_at: datetime) -> None:
    """监听主流程。任何异常都收敛实验（防永久卡 injected）。"""
    try:
        await _listen(exp_id, injected_at)
    except Exception:  # noqa: BLE001
        logger.exception("实验评估器异常 experiment=%s", exp_id)
        try:
            await _write_result(exp_id, None, notes="评估器异常，按未检出收敛")
        except Exception:  # noqa: BLE001
            logger.exception("实验异常收敛失败 experiment=%s", exp_id)


async def _listen(exp_id: int, injected_at: datetime) -> None:
    ev_id = await _await_association(exp_id, injected_at)
    if ev_id is None:
        await _write_result(
            exp_id,
            None,
            notes=f"注入后 {ASSOC_WINDOW_S // 60} 分钟内未产生可关联的告警事件（未检出本身即评估结论）",
        )
        return
    await _await_closure(exp_id, ev_id, injected_at)


async def _await_association(exp_id: int, injected_at: datetime) -> int | None:
    """关联窗口：轮询回填 experiment_id，返回关联到的事件 id（超窗返回 None）。"""
    t0 = injected_at - timedelta(seconds=_ASSOC_SKEW_S)
    deadline = injected_at + timedelta(seconds=ASSOC_WINDOW_S)
    while _utcnow() < deadline:
        async with SessionLocal() as db:
            exp = await db.get(Experiment, exp_id)
            if exp is None:
                return None
            namespace = exp.target_ns
            target_workload = exp.target_workload
            # 状态/时间/workload 匹配在 Python 侧过滤（与 runner.recover_orphans
            # 同风格；事件量级极小）
            stmt = select(FaultEvent).filter_by(namespace=namespace, experiment_id=None)
            rows = list((await db.execute(stmt)).scalars().all())
            candidates = [
                ev
                for ev in rows
                if ev.status in ACTIVE_STATUSES
                and (ev.workload == target_workload or ev.workload is None)
                and (ev.detected_at >= t0 or (ev.updated_at or ev.detected_at) >= t0)
            ]

        if candidates:
            picked = min(candidates, key=lambda ev: ev.detected_at)
            async with SessionLocal() as db:
                ev = await db.get(FaultEvent, picked.id)
                if ev is not None and ev.experiment_id is None:
                    ev.experiment_id = exp_id
                    ev.updated_at = _utcnow()
                    await db.commit()
                    logger.info(
                        "实验 %s 关联事件 #%s（%s/%s）", exp_id, ev.id, ev.namespace, ev.alert_name
                    )
                    return ev.id
        await asyncio.sleep(POLL_INTERVAL_S)
    return None


async def _await_closure(exp_id: int, ev_id: int, injected_at: datetime) -> None:
    """等事件到终态；总上限超时按当时状态快照评估。"""
    deadline = injected_at + timedelta(seconds=TOTAL_CAP_S)
    while True:
        async with SessionLocal() as db:
            ev = await db.get(FaultEvent, ev_id)
        if ev is None:
            await _write_result(exp_id, None, notes="关联事件已不存在，按未检出收敛")
            return
        if ev.status in TERMINAL_STATUSES:
            await _write_result(exp_id, ev_id)
            return
        if _utcnow() >= deadline:
            await _write_result(
                exp_id,
                ev_id,
                notes=(
                    f"等待闭环超时（{TOTAL_CAP_S // 60} 分钟）：事件仍处于 {ev.status} 状态，"
                    "按当前快照评估"
                ),
            )
            return
        await asyncio.sleep(POLL_INTERVAL_S)


async def _write_result(exp_id: int, ev_id: int | None, *, notes: str | None = None) -> None:
    """评估落库 + 实验置 finished（幂等：已有结果或已 finished 直接返回）。"""
    async with SessionLocal() as db:
        exp = await db.get(Experiment, exp_id)
        if exp is None:
            return
        experiment_id = exp_id
        stmt = select(ExperimentResult).filter_by(experiment_id=experiment_id)
        existing = list((await db.execute(stmt)).scalars().all())
        if existing or exp.status == "finished":
            return

        ev = await db.get(FaultEvent, ev_id) if ev_id else None
        detected = ev is not None
        latency = diagnosed = auto_recovered = mttr_s = None
        false_action = False
        auto_notes = None
        if detected:
            if exp.injected_at is not None:
                latency = max(0, int((ev.detected_at - exp.injected_at).total_seconds()))
            diag = await _latest_diagnosis(db, ev.id)
            prefixes = DIAGNOSIS_PREFIXES.get(exp.fault_type, ())
            if diag is None:
                diagnosed = None
            else:
                diagnosed = diag.fault_type.lower().startswith(prefixes)
            auto_recovered = ev.status == "resolved"
            mttr_s = ev.mttr_seconds
            false_action = await _has_false_action(db, ev, exp)
            auto_notes = _notes_for(ev, diag, latency)

        db.add(
            ExperimentResult(
                experiment_id=exp_id,
                fault_event_id=ev_id,
                detected=detected,
                detection_latency_s=latency,
                diagnosed_correctly=diagnosed,
                auto_recovered=auto_recovered,
                mttr_s=mttr_s,
                false_action=false_action,
                notes=notes or auto_notes,
            )
        )
        exp.status = "finished"
        await db.commit()
        logger.info("实验 %s 评估落库：detected=%s finished", exp_id, detected)

    # 收尾还原（finished 已落库；还原失败只记日志，phase3-plan §2.1 快照还原兜底）。
    # 有关联事件时延迟 4 分钟：事件可能被 webhook 提前置 resolved 而 Agent 验证
    # 窗口（3 分钟）仍在进行，立即还原的滚动更新会令 pod_ready 误判失败并诱发
    # 回滚（服务器实测竞态）。无关联事件（未检出）无此竞争，立即还原。
    if ev_id is not None:
        await asyncio.sleep(240)
    from faultlab import restore_experiment  # 局部导入避免循环依赖

    try:
        await restore_experiment(exp_id)
    except Exception:  # noqa: BLE001
        logger.exception("实验收尾还原失败 experiment=%s", exp_id)


async def _latest_diagnosis(db, fault_event_id: int) -> Diagnosis | None:
    stmt = select(Diagnosis).filter_by(fault_event_id=fault_event_id)
    diags = list((await db.execute(stmt)).scalars().all())
    return max(diags, key=lambda d: (d.created_at, d.id)) if diags else None


async def _has_false_action(db, ev: FaultEvent, exp: Experiment) -> bool:
    """误操作口径（phase3-plan §2.4）：该事件存在执行失败的修复动作，或注入
    后该目标 deployment 存在 rollback 失败审计（executor 审计 resource 固定）。"""
    fault_event_id = ev.id
    stmt = select(RemediationAction).filter_by(fault_event_id=fault_event_id)
    rems = list((await db.execute(stmt)).scalars().all())
    if any(r.status == "failed" for r in rems):
        return True
    if exp.injected_at is None:
        return False
    result = "failure"  # 审计量小，action/resource/时间在 Python 侧过滤
    stmt = select(AuditLog).filter_by(result=result)
    audits = list((await db.execute(stmt)).scalars().all())
    resource = resource_for("deployment", exp.target_ns, exp.target_workload)
    return any(
        a.action == "rollback" and a.created_at >= exp.injected_at and a.resource == resource
        for a in audits
    )


def _notes_for(ev: FaultEvent, diag: Diagnosis | None, latency: int | None) -> str:
    parts = []
    if latency is not None:
        parts.append(f"检测延迟 {latency}s")
    if diag is not None:
        parts.append(f"根因判定 {diag.fault_type}")
    if ev.status == "resolved":
        mttr = f"{ev.mttr_seconds}s" if ev.mttr_seconds is not None else "-"
        parts.append(f"自动恢复（MTTR {mttr}）")
    else:
        parts.append(f"事件终态 {ev.status}")
    return "；".join(parts)
