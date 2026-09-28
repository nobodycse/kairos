"""agent_runner（design.md §5.1/§5.2/§5.5）：触发、运行、恢复、孤儿扫描与落库。

进程内 asyncio（MVP 原则，§5 引言）：webhook/diagnose 调 trigger() 抢 Redis 锁
`event:{id}:running NX EX 1800` 后台起图；approve/reject 调 spawn_resume() 以
Command(resume={"approved": bool}) 恢复挂起图。RCA/提案落库、终态状态迁移、
审计在这里；中间态迁移（verifying/rolling_back/diagnosing）在图节点。

线程轮次：thread_id = `fault-{id}-r{rediagnose_count}`——回滚重诊断由 runner
重入新线程实现（evidence 是追加式 reducer，同线程重跑会污染上下文，
落地决策 §一.9）。
"""
import asyncio
import logging
from datetime import datetime, timezone

from langgraph.types import Command
from sqlalchemy import select

from agent import events
from agent.graph import graph, thread_id_for
from agent.schemas import RemediationPlan, RCAReport
from core.config import settings
from core.db import SessionLocal
from core.redis import r as redis
from models import ACTIVE_STATUSES, Diagnosis, FaultEvent, RemediationAction
from risk_control.audit import write_audit
from system_settings import llm_ready

logger = logging.getLogger(__name__)

_LOCK_TTL = 1800  # §5.5：event:{id}:running TTL
_FAIL_STREAK_KEY = "agent:consecutive_failures"
_FAIL_STREAK_ALERT = 5  # §5.4：连续 5 个事件失败 → 告警日志
_FAIL_STREAK_TTL = 3600
# §5.1 孤儿清单（diagnosing/remediating/verifying）+ awaiting_approval
#（MemorySaver 决策下重启后无法 resume，等价孤儿——phase2-plan §三.1）
_ORPHAN_STATUSES = ("diagnosing", "remediating", "verifying", "awaiting_approval")

_tasks: set[asyncio.Task] = set()  # 持引用防 GC（asyncio.create_task 陷阱）


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _lock_key(fault_event_id: int) -> str:
    return f"event:{fault_event_id}:running"


# ---------- 触发 ----------


async def trigger(fault_event_id: int) -> bool:
    """抢锁成功则后台运行诊断图；返回是否触发（§5.3.3/§5.5）。

    抢锁失败 = 该事件已有 graph 在跑：webhook 静默跳过，diagnose 映射 409。
    """
    setted = await redis.set(_lock_key(fault_event_id), "1", nx=True, ex=_LOCK_TTL)
    if not setted:
        logger.info("trigger 跳过：事件 %s 已在运行（锁占用）", fault_event_id)
        return False
    _spawn(run(fault_event_id))
    return True


# ---------- 运行 ----------


async def run(fault_event_id: int) -> None:
    """运行诊断图并按终态落库（调用方已持锁）。未捕获异常 → failed + audit（§5.1）。"""
    try:
        outcome = await _run_inner(fault_event_id)
    except Exception as e:
        logger.exception("诊断失败 fault_event_id=%s", fault_event_id)
        await _mark_failed(fault_event_id, f"{type(e).__name__}: {e}"[:500])
        await _bump_fail_streak()
        await redis.delete(_lock_key(fault_event_id))
        return
    await _finish(fault_event_id, outcome)


async def _finish(fault_event_id: int, outcome: str | None) -> None:
    """终态收尾：放锁 + 连续失败计数 + redo 重入新线程（落地决策 §一.9）。"""
    await redis.delete(_lock_key(fault_event_id))
    if outcome == "redo":
        # 回滚节点已置 diagnosing 并递增 rediagnose_count
        if not await trigger(fault_event_id):
            logger.error("重诊断重入抢锁失败 fault_event_id=%s（不应发生）", fault_event_id)
        return
    if outcome == "resolved":
        await _clear_fail_streak()
    elif outcome in ("exec_failed", "failed"):
        await _bump_fail_streak()


async def _run_inner(fault_event_id: int) -> str | None:
    async with SessionLocal() as db:
        ev = await db.get(FaultEvent, fault_event_id)
        if ev is None:
            logger.warning("run：事件不存在 id=%s", fault_event_id)
            return None
        if not await llm_ready(db):
            # 前置检查（Phase 2.5）：未配置 AI 时直接 failed 并给出可操作提示
            await _mark_failed(
                fault_event_id, "LLM 未配置：请在网页「系统设置」中配置 AI 供应商后重新触发诊断"
            )
            return None
        if ev.status != "diagnosing":
            await events.emit_status_changed(fault_event_id, ev.status, "diagnosing", "agent 开始诊断")
        ev.status = "diagnosing"
        ev.updated_at = datetime.now(timezone.utc)
        await db.commit()
        alert = {
            "alert_name": ev.alert_name,
            "severity": ev.severity,
            "namespace": ev.namespace,
            "workload": ev.workload,
            "labels": ev.labels or {},
        }
        rediagnose_count = ev.rediagnose_count or 0

    init: dict = {
        "fault_event_id": fault_event_id,
        "alert": alert,
        "evidence": [],
        "hypotheses": [],
        "rca": None,
        "plan": None,
        "risk_decision": None,
        "iteration": 0,
        "rediagnose_count": rediagnose_count,
        "outcome": None,
    }
    config = {"configurable": {"thread_id": thread_id_for(fault_event_id, rediagnose_count)}}
    result = await graph.ainvoke(init, config=config)
    interrupted = "__interrupt__" in result
    async with SessionLocal() as db:
        return await _finalize(
            db, fault_event_id, result, interrupted=interrupted, approved=None
        )


async def _finalize(
    db,
    fault_event_id: int,
    result: dict,
    *,
    interrupted: bool,
    approved: bool | None,
) -> str | None:
    """按图终态落库，返回 outcome 供 _finish 处理锁/计数/重入。"""
    state = {k: v for k, v in result.items() if k != "__interrupt__"}
    ev = await db.get(FaultEvent, fault_event_id)
    if ev is None:
        logger.warning("finalize：事件不存在 id=%s", fault_event_id)
        return None
    now = datetime.now(timezone.utc)
    ev.updated_at = now

    if interrupted:
        # 先落库后置状态：前端弹确认框时 §3.6 详情已有数据
        rca: RCAReport | None = state.get("rca")
        plan: RemediationPlan | None = state.get("plan")
        decision = state.get("risk_decision")
        if rca is not None:
            db.add(
                Diagnosis(
                    fault_event_id=fault_event_id,
                    fault_type=rca.fault_type,
                    root_cause=rca.root_cause,
                    evidence=[e.model_dump() for e in rca.evidence],
                    confidence=rca.confidence,
                    blast_radius=rca.blast_radius,
                    suggestion=rca.suggestion,
                    llm_model=settings.llm_model,
                    iterations=state.get("iteration") or 0,
                )
            )
        if plan is not None and decision is not None:
            db.add(
                RemediationAction(
                    fault_event_id=fault_event_id,
                    action=plan.action,
                    namespace=plan.namespace,
                    target=plan.target,
                    params=plan.params,
                    risk_level=decision.risk_level.value,
                    policy=decision.policy.value,
                    status="pending",
                )
            )
        await db.commit()
        if ev.status != "awaiting_approval":
            await events.emit_status_changed(
                fault_event_id, ev.status, "awaiting_approval", "方案生成，需人工确认"
            )
            ev.status = "awaiting_approval"
            await db.commit()
        return None

    if approved is False:
        # reject：approve/reject 接口已置 closed，这里仅一致性兜底
        if ev.status != "closed":
            await events.emit_status_changed(fault_event_id, ev.status, "closed", "人工拒绝修复")
            ev.status = "closed"
            await db.commit()
        return None

    outcome = state.get("outcome")
    if outcome == "resolved":
        if ev.status != "resolved":
            await events.emit_status_changed(fault_event_id, ev.status, "resolved", "验证通过，故障恢复")
            ev.status = "resolved"
            ev.resolved_at = now
            ev.mttr_seconds = max(0, int((now - ev.detected_at).total_seconds()))
            await db.commit()
        return "resolved"
    if outcome in ("exec_failed", "failed"):
        if ev.status in ACTIVE_STATUSES:
            reason = "修复执行失败" if outcome == "exec_failed" else "回滚未完成或重诊断次数用尽"
            await events.emit_status_changed(fault_event_id, ev.status, "failed", reason)
            ev.status = "failed"
            await db.commit()
        return outcome
    if outcome == "redo":
        return "redo"  # 回滚节点已置 diagnosing + rediagnose_count

    # 无 outcome：gate 分支拒绝（FORBIDDEN / 白名单不通过）→ failed 转人工
    if ev.status in ACTIVE_STATUSES:
        decision = state.get("risk_decision")
        reason = decision.reason if decision is not None else "方案未通过风险门控"
        await events.emit_status_changed(fault_event_id, ev.status, "failed", f"方案被拒绝：{reason}")
        ev.status = "failed"
        await db.commit()
        await write_audit(
            db,
            actor="agent",
            action="diagnose",
            resource=f"fault_event/{fault_event_id}",
            result="failure",
            detail={"reason": reason},
        )
    return "failed"


# ---------- 恢复（approve/reject） ----------


def spawn_resume(fault_event_id: int, approved: bool) -> None:
    """approve/reject 接口入口：后台恢复挂起图（design.md §5.2）。"""
    _spawn(resume(fault_event_id, approved))


async def resume(fault_event_id: int, approved: bool) -> None:
    async with SessionLocal() as db:
        ev = await db.get(FaultEvent, fault_event_id)
        round_count = (ev.rediagnose_count or 0) if ev is not None else 0
    config = {"configurable": {"thread_id": thread_id_for(fault_event_id, round_count)}}
    try:
        snap = graph.get_state(config)
    except Exception:
        logger.exception("resume 读取图状态失败 fault_event_id=%s", fault_event_id)
        return
    if not snap.next:
        logger.warning(
            "resume：事件 %s 无挂起图（重启后已被孤儿扫描处置或已恢复）", fault_event_id
        )
        return
    try:
        result = await graph.ainvoke(Command(resume={"approved": approved}), config=config)
    except Exception as e:
        logger.exception("resume 失败 fault_event_id=%s", fault_event_id)
        await _mark_failed(fault_event_id, f"resume: {type(e).__name__}: {e}"[:500])
        return
    async with SessionLocal() as db:
        outcome = await _finalize(
            db, fault_event_id, result, interrupted=False, approved=approved
        )
    await _finish(fault_event_id, outcome)


# ---------- 失败路径与计数 ----------


async def _mark_failed(fault_event_id: int, error: str) -> None:
    """事件置 failed + audit（§5.1）。已是终态（closed/resolved）不降级。"""
    try:
        async with SessionLocal() as db:
            ev = await db.get(FaultEvent, fault_event_id)
            if ev is None or ev.status not in ACTIVE_STATUSES:
                return
            await events.emit_status_changed(fault_event_id, ev.status, "failed", f"诊断失败：{error}")
            ev.status = "failed"
            ev.updated_at = datetime.now(timezone.utc)
            await db.commit()
            await write_audit(
                db,
                actor="agent",
                action="diagnose",
                resource=f"fault_event/{fault_event_id}",
                result="failure",
                detail={"error": error},
            )
    except Exception:
        logger.exception("置 failed 失败 fault_event_id=%s", fault_event_id)


async def _bump_fail_streak() -> None:
    try:
        count = await redis.incr(_FAIL_STREAK_KEY)
        await redis.expire(_FAIL_STREAK_KEY, _FAIL_STREAK_TTL)
        if count >= _FAIL_STREAK_ALERT:
            logger.error("告警：连续 %s 个事件诊断失败（§5.4），请检查 LLM/依赖", count)
    except Exception:
        logger.exception("连续失败计数异常")


async def _clear_fail_streak() -> None:
    try:
        await redis.delete(_FAIL_STREAK_KEY)
    except Exception:
        pass


# ---------- 孤儿扫描（启动时） ----------


async def recover_orphans() -> int:
    """启动扫描（§5.1）：孤儿事件统一置 failed，不自动续跑。

    孤儿 = diagnosing/remediating/verifying（§5.1 原文）+ awaiting_approval
    （MemorySaver 决策下重启后无法 resume，phase2-plan §三.1 扩展）。
    """
    pairs: list[tuple[int, str]] = []
    async with SessionLocal() as db:
        rows = list((await db.execute(select(FaultEvent))).scalars().all())
        orphans = [ev for ev in rows if ev.status in _ORPHAN_STATUSES]
        now = datetime.now(timezone.utc)
        for ev in orphans:
            pairs.append((ev.id, ev.status))
            ev.status = "failed"
            ev.updated_at = now
        await db.commit()
        for event_id, prev in pairs:
            await write_audit(
                db,
                actor="system",
                action="orphan_scan",
                resource=f"fault_event/{event_id}",
                result="failure",
                detail={"reason": "backend restart orphan", "previous_status": prev},
            )
    if pairs:
        logger.warning("孤儿扫描：重启孤儿事件置 failed %s", [p[0] for p in pairs])
    return len(pairs)
