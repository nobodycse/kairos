"""故障事件：列表 / 详情 / 触发诊断 / SSE 流 / 人工确认（design.md §3.5–§3.8、§4）。

Phase 1：列表/详情接 fault_events 真数据（§3.5/§3.6）；SSE 保持演示序列
（真实时线 Phase 2 阶段三随 SSE 真实化落地）。Phase 2 阶段二：diagnose 真实
触发 agent_runner（§5.1/§5.5），approve/reject 补 approved_by/审计并恢复挂起图
（§5.2）。

查询走 ORM 参数化表达式；事件量级小，过滤/排序/分页在 Python 侧完成
（与 cluster 路由的内存分页风格一致）。
"""
import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent import runner as agent_runner
from api import mock
from api.deps import require_user
from core.db import get_db
from core.security import decode_access_token
from models import ACTIVE_STATUSES, Diagnosis, FaultEvent, RemediationAction, User
from risk_control.audit import resource_for, write_audit

router = APIRouter(tags=["faults"])

# 对 diagnose 而言的已结束状态：resolved/closed（§3.7「该事件已结束」）。
# failed 不在其列——§5.1 明文孤儿置 failed 后"人工可 §3.7 重新触发"。
_ENDED_STATES = {"resolved", "closed"}
# 活跃但非诊断中的状态：重入被拒（防同事件多图，配合 Redis 锁双保险）
_ACTIVE_BLOCKING = ("awaiting_approval", "remediating", "verifying", "rolling_back")
_ALL_STATES = set(ACTIVE_STATUSES) | {"resolved", "failed", "closed"}


class _DecisionBody(BaseModel):
    """approve/reject 可选请求体（§3.8：comment 仅记录进审计）。"""

    comment: str | None = None


async def _user_id(db: AsyncSession, username: str) -> int | None:
    """username → users.id（users 表量级小，Python 侧过滤；Mimosa 对 where(==) 有误报）。"""
    rows = list((await db.execute(select(User))).scalars().all())
    return next((u.id for u in rows if u.username == username), None)


def _list_item(ev: FaultEvent) -> dict:
    """§3.5 列表条目投影。"""
    return {
        "id": ev.id,
        "alert_name": ev.alert_name,
        "severity": ev.severity,
        "namespace": ev.namespace,
        "workload": ev.workload,
        "status": ev.status,
        "detected_at": ev.detected_at.isoformat() if ev.detected_at else None,
        "resolved_at": ev.resolved_at.isoformat() if ev.resolved_at else None,
        "mttr_seconds": ev.mttr_seconds,
    }


def _paginate(items: list, page: int, page_size: int) -> dict:
    return {
        "items": items[(page - 1) * page_size : page * page_size],
        "total": len(items),
        "page": page,
        "page_size": page_size,
    }


async def _faults_by(db: AsyncSession, status: str | None, namespace: str | None) -> list[FaultEvent]:
    """全量取回后 Python 侧过滤排序（事件量级小，见模块注释）。"""
    rows = list((await db.execute(select(FaultEvent))).scalars().all())
    if status == "active":
        rows = [ev for ev in rows if ev.status in ACTIVE_STATUSES]
    elif status:
        rows = [ev for ev in rows if ev.status == status]
    if namespace:
        rows = [ev for ev in rows if ev.namespace == namespace]
    rows.sort(key=lambda ev: ev.detected_at, reverse=True)  # §3.5：detected_at 倒序
    return rows


@router.get("/faults", summary="故障事件列表（status 支持枚举值或 active，namespace 过滤）")
async def list_faults(
    status: str | None = None,
    namespace: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_user),
):
    if status and status != "active" and status not in _ALL_STATES:
        raise HTTPException(status_code=400, detail=f"非法 status: {status}")
    rows = await _faults_by(db, status, namespace)
    return _paginate([_list_item(ev) for ev in rows], page, page_size)


def _diagnosis_dict(d: Diagnosis) -> dict:
    return {
        "id": d.id,
        "fault_type": d.fault_type,
        "root_cause": d.root_cause,
        "evidence": d.evidence or [],
        "confidence": d.confidence,
        "blast_radius": d.blast_radius,
        "suggestion": d.suggestion,
        "llm_model": d.llm_model,
        "iterations": d.iterations,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def _remediation_dict(r: RemediationAction) -> dict:
    return {
        "id": r.id,
        "action": r.action,
        "namespace": r.namespace,
        "target": r.target,
        "params": r.params or {},
        "risk_level": r.risk_level,
        "policy": r.policy,
        "status": r.status,
        "approved_by": r.approved_by,
        "snapshot": r.snapshot,
        "executed_at": r.executed_at.isoformat() if r.executed_at else None,
    }


async def _load_detail(db: AsyncSession, ev: FaultEvent) -> dict:
    """§3.6 详情：一次取全（diagnosis 取最新一条，remediations 按创建正序）。"""
    detail = _list_item(ev)
    detail.update(
        {
            "fingerprint": ev.fingerprint,
            "labels": ev.labels or {},
            "rediagnose_count": ev.rediagnose_count,
            "experiment_id": ev.experiment_id,
            "diagnosis": None,
            "remediations": [],
        }
    )
    diag_all = list((await db.execute(select(Diagnosis))).scalars().all())
    diag_rows = [d for d in diag_all if d.fault_event_id == ev.id]
    if diag_rows:
        latest = max(diag_rows, key=lambda d: d.created_at)
        detail["diagnosis"] = _diagnosis_dict(latest)
    rem_all = list((await db.execute(select(RemediationAction))).scalars().all())
    rem_rows = [r for r in rem_all if r.fault_event_id == ev.id]
    rem_rows.sort(key=lambda r: r.created_at)
    detail["remediations"] = [_remediation_dict(r) for r in rem_rows]
    return detail


@router.get("/faults/{fault_id}", summary="事件详情（诊断中时 diagnosis 为 null）")
async def get_fault(
    fault_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_user),
):
    ev = await db.get(FaultEvent, fault_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="故障事件不存在")
    return await _load_detail(db, ev)


@router.post("/faults/{fault_id}/diagnose", status_code=202, summary="手动（重新）触发诊断")
async def diagnose(
    fault_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_user),
):
    ev = await db.get(FaultEvent, fault_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="故障事件不存在")
    if ev.status in _ENDED_STATES:
        raise HTTPException(status_code=409, detail="该事件已结束")
    if ev.status == "diagnosing":
        raise HTTPException(status_code=409, detail="该事件正在诊断中")
    if ev.status in _ACTIVE_BLOCKING:
        # 契约只定义两种 409 文案；此处为同族补充（awaiting_approval 等重入拒绝）
        raise HTTPException(status_code=409, detail="该事件正在处理中")
    # detected / failed（§5.1：failed 可人工重新触发）→ 抢锁后进程内起图
    triggered = await agent_runner.trigger(fault_id)
    if not triggered:
        raise HTTPException(status_code=409, detail="该事件正在诊断中")
    return {"fault_event_id": fault_id, "status": "diagnosing"}


@router.post("/remediations/{remediation_id}/approve", summary="人工批准修复")
async def approve(
    remediation_id: int,
    body: _DecisionBody | None = None,
    db: AsyncSession = Depends(get_db),
    username: str = Depends(require_user),
):
    rem = await db.get(RemediationAction, remediation_id)
    if rem is None:
        raise HTTPException(status_code=404, detail="修复动作不存在")
    if rem.status != "pending":
        raise HTTPException(status_code=409, detail="该动作当前状态不允许此操作")
    rem.status = "approved"
    rem.approved_by = await _user_id(db, username)
    ev = await db.get(FaultEvent, rem.fault_event_id)
    if ev is not None:
        ev.status = "remediating"
        ev.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await write_audit(
        db,
        actor=f"user:{username}",
        action="approve",
        resource=resource_for("remediation", rem.namespace, rem.target),
        params=rem.params or {},
        result="success",
        detail={
            "comment": body.comment if body else None,
            "remediation_id": remediation_id,
            "fault_event_id": rem.fault_event_id,
        },
    )
    # §5.2：Command(resume={"approved": True}) 恢复挂起图（阶段二恢复后走交接桩）
    agent_runner.spawn_resume(rem.fault_event_id, approved=True)
    return {"id": remediation_id, "status": "approved", "fault_event_status": "remediating"}


@router.post("/remediations/{remediation_id}/reject", summary="人工拒绝修复")
async def reject(
    remediation_id: int,
    body: _DecisionBody | None = None,
    db: AsyncSession = Depends(get_db),
    username: str = Depends(require_user),
):
    rem = await db.get(RemediationAction, remediation_id)
    if rem is None:
        raise HTTPException(status_code=404, detail="修复动作不存在")
    if rem.status != "pending":
        raise HTTPException(status_code=409, detail="该动作当前状态不允许此操作")
    rem.status = "rejected"
    ev = await db.get(FaultEvent, rem.fault_event_id)
    if ev is not None:
        ev.status = "closed"
        ev.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await write_audit(
        db,
        actor=f"user:{username}",
        action="reject",
        resource=resource_for("remediation", rem.namespace, rem.target),
        params=rem.params or {},
        result="success",
        detail={
            "comment": body.comment if body else None,
            "remediation_id": remediation_id,
            "fault_event_id": rem.fault_event_id,
        },
    )
    agent_runner.spawn_resume(rem.fault_event_id, approved=False)  # 恢复图走 closed 收尾
    return {"id": remediation_id, "status": "rejected", "fault_event_status": "closed"}


def _sse_frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get(
    "/faults/{fault_id}/stream",
    summary="SSE 事件流（契约见 design.md §4，/docs 覆盖不了 SSE）",
    response_class=StreamingResponse,
)
async def stream(
    fault_id: int,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    # EventSource 不能设置请求头，JWT 走 query 参数（design.md §4.1）
    if decode_access_token(token) is None:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    if await db.get(FaultEvent, fault_id) is None:
        raise HTTPException(status_code=404, detail="故障事件不存在")

    async def event_stream():
        # 连接/重连先发 snapshot（design.md §4.2）；Phase 2 接真实 agent 事件
        yield _sse_frame("snapshot", mock.SSE_SNAPSHOT)
        # 演示性 agent_step 序列：2s 一帧，tool_start/tool_end 成对（§4.4）
        for step in mock.SSE_DEMO_STEPS:
            await asyncio.sleep(2)
            yield _sse_frame("agent_step", {**step, "fault_event_id": fault_id})
        await asyncio.sleep(2)
        yield _sse_frame("status_changed", mock.SSE_STATUS_CHANGED)
        await asyncio.sleep(2)
        # 演示 verification_progress 事件类型；真实时序为批准 → verifying 后才有
        yield _sse_frame("verification_progress", mock.SSE_VERIFICATION)
        # 15s 注释心跳，防代理断连（§4.1）
        while True:
            await asyncio.sleep(15)
            yield ": ping\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
