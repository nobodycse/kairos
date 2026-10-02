"""故障实验室路由（design.md §3.9–§3.11 + Phase 3 新增列表/对比端点）。

全部真 DB（Phase 0 mock 已随本阶段删除）；注入走 faultlab 后台状态机，
本层只做契约校验、互斥预检与查询组织。fault_type 契约枚举保留 7 种
（§1.2），本阶段仅 3 种可注入，其余 422"暂不支持注入"。
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from agent.tools import _GLOBAL_EXPRS  # 与告警规则同源的 PromQL（compare 复用）
from api.deps import require_user
from core.config import settings
from core.db import get_db
from faultlab import running_experiment, spawn_inject
from models import Experiment, ExperimentResult, FaultEvent
from monitoring import clients

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/experiments", tags=["experiments"])

FaultType = Literal[
    "oom",
    "cpu_overload",
    "pod_crash",
    "image_pull_backoff",
    "replica_anomaly",
    "network_latency",
    "node_not_ready",
]

SUPPORTED_FAULT_TYPES = ("oom", "pod_crash", "cpu_overload")  # phase3-plan §2.8 砍单
_LIST_LIMIT = 50
_RECOVERY_WINDOW_S = 600  # 恢复窗 = resolved_at → +10m（§3.11.2）
_MIN_STEP_S = 30  # 对比曲线采样下限；窗口越长步长自动放大，控制点数 ≤~120


class ExperimentCreate(BaseModel):
    fault_type: FaultType
    target_workload: str
    params: dict = {}


def _public_params(params: dict | None) -> dict:
    """剥离下划线前缀的内部键（_snapshot/_stress_pod/_last_error 等）。"""
    return {k: v for k, v in (params or {}).items() if not k.startswith("_")}


def _result_payload(result: ExperimentResult | None) -> dict | None:
    if result is None:
        return None
    return {
        "detected": result.detected,
        "detection_latency_s": result.detection_latency_s,
        "diagnosed_correctly": result.diagnosed_correctly,
        "auto_recovered": result.auto_recovered,
        "mttr_s": result.mttr_s,
        "false_action": result.false_action,
        "notes": result.notes,
    }


def _exp_payload(
    exp: Experiment, result: ExperimentResult | None, assoc_event_id: int | None = None
) -> dict:
    return {
        "id": exp.id,
        "fault_type": exp.fault_type,
        "target_ns": exp.target_ns,
        "target_workload": exp.target_workload,
        "params": _public_params(exp.params),
        "status": exp.status,
        "injected_at": exp.injected_at,
        "created_at": exp.created_at,
        "last_error": (exp.params or {}).get("_last_error"),
        "fault_event_id": (result.fault_event_id if result else None) or assoc_event_id,
        "result": _result_payload(result),
    }


async def _latest_result(db, experiment_id: int) -> ExperimentResult | None:
    """最新评估结果（评估器幂等，正常只有一条；防御重复行取最新）。"""
    experiment_id_ = experiment_id
    stmt = select(ExperimentResult).filter_by(experiment_id=experiment_id_)
    rows = list((await db.execute(stmt)).scalars().all())
    return max(rows, key=lambda r: (r.created_at, r.id)) if rows else None


async def _associated_event_id(db, experiment_id: int) -> int | None:
    """已回填关联的事件 id（评估收敛前即暴露，§3.11 注记）。"""
    experiment_id_ = experiment_id
    stmt = select(FaultEvent).filter_by(experiment_id=experiment_id_)
    rows = list((await db.execute(stmt)).scalars().all())
    return rows[0].id if rows else None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@router.post("", status_code=201, summary="创建故障实验（§3.9）")
async def create_experiment(
    req: ExperimentCreate, db=Depends(get_db), _: str = Depends(require_user)
):
    if req.fault_type not in SUPPORTED_FAULT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"故障类型 {req.fault_type} 暂不支持注入（当前支持：oom / pod_crash / cpu_overload）",
        )
    target = req.target_workload.strip()
    if not target:
        raise HTTPException(status_code=422, detail="target_workload 不能为空")
    # oom 走调低 limit 方式时传 params.memory_limit（§3.9）；下划线键为内部保留
    params = {k: v for k, v in req.params.items() if not str(k).startswith("_")}
    exp = Experiment()
    exp.fault_type = req.fault_type
    exp.target_ns = settings.demo_namespace
    exp.target_workload = target
    exp.params = params
    exp.status = "created"
    db.add(exp)
    await db.commit()
    return {
        "id": exp.id,
        "fault_type": exp.fault_type,
        "target_ns": exp.target_ns,
        "target_workload": exp.target_workload,
        "params": _public_params(exp.params),
        "status": exp.status,
        "injected_at": exp.injected_at,
        "created_at": exp.created_at,
    }


@router.post("/{experiment_id}/inject", status_code=202, summary="执行注入（§3.10，后台状态机）")
async def inject(
    experiment_id: int, db=Depends(get_db), _: str = Depends(require_user)
):
    exp = await db.get(Experiment, experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="实验不存在")
    if exp.status != "created":
        raise HTTPException(
            status_code=409, detail=f"实验当前状态 {exp.status} 不允许注入"
        )
    other = await running_experiment(db, exclude_id=experiment_id)
    if other is not None:
        raise HTTPException(
            status_code=409,
            detail=f"已有进行中的实验 #{other.id}（同一时刻只允许一个，phase3-plan §2.6）",
        )
    spawn_inject(experiment_id)
    return {"id": experiment_id, "status": "injecting"}


@router.get("", summary="实验列表（Phase 3 新增，id 倒序最多 50 条，§3.11.1）")
async def list_experiments(db=Depends(get_db), _: str = Depends(require_user)):
    stmt = select(Experiment)
    rows = list((await db.execute(stmt)).scalars().all())
    rows.sort(key=lambda e: e.id, reverse=True)
    total = len(rows)
    items = []
    for exp in rows[:_LIST_LIMIT]:
        items.append(
            _exp_payload(
                exp,
                await _latest_result(db, exp.id),
                await _associated_event_id(db, exp.id),
            )
        )
    return {"items": items, "total": total}


@router.get("/{experiment_id}/report", summary="实验报告（§3.11，未闭环 result 为 null）")
async def report(experiment_id: int, db=Depends(get_db), _: str = Depends(require_user)):
    exp = await db.get(Experiment, experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="实验不存在")
    return _exp_payload(
        exp,
        await _latest_result(db, experiment_id),
        await _associated_event_id(db, experiment_id),
    )


# ---------- compare（§3.11.2，Phase 3 新增） ----------

_COMPARE_METRICS = (
    ("error_rate", "5xx 错误率", "ratio", _GLOBAL_EXPRS["error_rate"]),
    ("p95_latency", "P95 延迟", "seconds", _GLOBAL_EXPRS["p95_latency"]),
    # CPU/内存按事件 Pod 前缀匹配（labels.pod 优先，缺省退回 target_workload）
    ("cpu_usage", "CPU 使用", "cores", None),
    ("memory_usage", "内存使用", "bytes", None),
)


def _expr_for(key: str, selector: str) -> str:
    if key == "cpu_usage":
        return f"sum(rate(container_cpu_usage_seconds_total{{{selector}}}[2m]))"
    return f"sum(container_memory_working_set_bytes{{{selector}}})"


def _step_for(start: datetime, end: datetime) -> int:
    span = max(0, int((end - start).total_seconds()))
    return max(_MIN_STEP_S, span // 120)


async def _window_points(expr: str, start: datetime, end: datetime) -> list[dict]:
    """单窗口序列（Prometheus 软依赖：失败返回空 points，§3.2.1 同语义）。"""
    try:
        series = await clients.prometheus.query_range(
            expr, start.timestamp(), end.timestamp(), _step_for(start, end)
        )
    except Exception:  # noqa: BLE001
        logger.warning("compare 查询失败（软依赖降级）：%s", expr)
        return []
    pts: list[tuple[float, float]] = []
    for s in series:  # sum() 聚合正常单序列；防御多条
        pts.extend(s.values)
    pts.sort()
    return [
        {"ts": _iso(datetime.fromtimestamp(ts, tz=timezone.utc)), "value": round(v, 6)}
        for ts, v in pts
    ]


@router.get("/{experiment_id}/compare", summary="修复前后指标对比（Phase 3 新增，§3.11.2）")
async def compare(experiment_id: int, db=Depends(get_db), _: str = Depends(require_user)):
    exp = await db.get(Experiment, experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="实验不存在")
    result = await _latest_result(db, experiment_id)
    if result is None or result.fault_event_id is None:
        raise HTTPException(status_code=409, detail="实验尚未关联故障事件，无法生成对比")
    ev = await db.get(FaultEvent, result.fault_event_id)
    if ev is None or ev.status != "resolved" or ev.resolved_at is None:
        raise HTTPException(
            status_code=409, detail="关联事件尚未恢复（resolved），恢复后才能生成对比"
        )

    fault_start, fault_end = ev.detected_at, ev.resolved_at
    recov_start = ev.resolved_at
    recov_end = ev.resolved_at + timedelta(seconds=_RECOVERY_WINDOW_S)
    prefix = (ev.labels or {}).get("pod") or exp.target_workload
    selector = f'namespace="{exp.target_ns}", pod=~"{re.escape(prefix)}.*"'

    async def both_windows(key: str, expr: str) -> dict:
        fault_pts, recov_pts = await asyncio.gather(
            _window_points(expr, fault_start, fault_end),
            _window_points(expr, recov_start, recov_end),
        )
        return {"fault": {"points": fault_pts}, "recovery": {"points": recov_pts}}

    tasks = [
        both_windows(key, expr if expr else _expr_for(key, selector))
        for key, _name, _unit, expr in _COMPARE_METRICS
    ]
    windows = await asyncio.gather(*tasks)
    metrics = [
        {"key": key, "name": name, "unit": unit, **window}
        for (key, name, unit, _expr), window in zip(_COMPARE_METRICS, windows)
    ]
    return {
        "experiment_id": exp.id,
        "fault_event_id": ev.id,
        "fault_window": {"start": _iso(fault_start), "end": _iso(fault_end)},
        "recovery_window": {"start": _iso(recov_start), "end": _iso(recov_end)},
        "metrics": metrics,
    }
