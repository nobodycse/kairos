"""Alertmanager 告警接收（design.md §3.13/§5.3，无鉴权，仅内网可达）。

处理链：fingerprint 去重（Redis SET NX EX 3600）→ 归并（namespace+workload
活跃事件，labels 并集 / severity 取高 / 保留最早 detected_at）→ fault_events
落库。resolved 告警不改状态机之外的东西：合入既有事件并置 resolved + MTTR。
Phase 2 在落库后接 agent_runner 触发诊断（§5.1，此处留 TODO）。

查询全部走 SQLAlchemy ORM 参数化表达式（filter_by 编译为占位符绑定）；
事件量级极小，排序在 Python 侧完成（与 cluster 路由的内存分页风格一致）。
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.db import get_db
from core.redis import r as redis
from models import ACTIVE_STATUSES, FaultEvent
from monitoring import clients

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_FINGERPRINT_TTL = 3600  # §5.3.1：指纹去重窗口
_FINGERPRINT_KEY = "alerts:"  # + fingerprint
_SEVERITY_RANK = {"warning": 0, "critical": 1}


async def _active_event_for(db: AsyncSession, namespace: str, workload: str | None) -> FaultEvent | None:
    """查同 namespace+workload 的活跃事件（部分索引 idx_fault_events_active_ns_wl）。

    filter_by 对 None 自动生成 IS NULL，正好覆盖 workload 未知的归并场景。
    返回 detected_at 最早的一条（归并目标）。
    """
    stmt = (
        select(FaultEvent)
        .filter_by(namespace=namespace, workload=workload)
        .where(FaultEvent.status.in_(ACTIVE_STATUSES))
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return min(rows, key=lambda ev: ev.detected_at) if rows else None


async def _handle_firing(db: AsyncSession, alert: dict, counts: dict) -> None:
    labels = alert.get("labels") or {}
    fingerprint = str(alert.get("fingerprint") or "")
    alert_name = labels.get("alertname") or "UnknownAlert"
    severity = labels.get("severity") if labels.get("severity") in _SEVERITY_RANK else "warning"

    # §5.3.1 指纹去重：重复通知（repeat_interval 30m 内）直接 ignored
    if fingerprint:
        dedup_key = _FINGERPRINT_KEY + fingerprint
        setted = await redis.set(dedup_key, "1", nx=True, ex=_FINGERPRINT_TTL)
        if not setted:
            counts["ignored"] += 1
            return

    # §5.3.2 归并键：namespace + workload。聚合类告警（如 HTTPErrorRateHigh）
    # 不带 namespace/pod 标签，namespace 回退 demo_namespace，workload 为 None
    namespace = labels.get("namespace") or settings.demo_namespace
    pod = labels.get("pod")
    workload = None
    if pod:
        try:
            workload = await clients.k8s.workload_of_pod(namespace, pod)
        except Exception:  # noqa: BLE001  K8s 不可达不阻断落库
            logger.warning("workload 推导失败 ns=%s pod=%s", namespace, pod)

    try:
        now = datetime.now(timezone.utc)
        existing = await _active_event_for(db, namespace, workload)
        if existing is not None:
            # 合并：labels 并集、severity 取高、保留最早 detected_at
            existing.labels = {**(existing.labels or {}), **labels}
            if _SEVERITY_RANK[severity] > _SEVERITY_RANK.get(existing.severity, 0):
                existing.severity = severity
            existing.updated_at = now
            await db.commit()
            counts["merged"] += 1
            # TODO(Phase 2 §5.1): agent_runner 触发（Redis 抢锁 SET event:{id}:running NX EX 1800）
        else:
            ev = FaultEvent()
            ev.fingerprint = fingerprint
            ev.alert_name = alert_name
            ev.severity = severity
            ev.namespace = namespace
            ev.workload = workload
            ev.labels = labels
            ev.status = "detected"
            ev.detected_at = now
            ev.updated_at = now
            db.add(ev)
            await db.commit()
            counts["created"] += 1
            # TODO(Phase 2 §5.1): 同上
    except Exception:
        # 落库失败时撤销指纹占用，让下一次 webhook 能重试（否则 1h 内全被 ignored）
        if fingerprint:
            await redis.delete(_FINGERPRINT_KEY + fingerprint)
        raise


async def _handle_resolved(db: AsyncSession, alert: dict, counts: dict) -> None:
    """resolved：合入既有事件置 resolved + MTTR，不新建（§3.13）。

    计入 merged（语义：并入既有事件并关闭）。事件不存在（webhook 丢失或
    指纹 TTL 已过）则忽略；TODO(Phase 2 §6.7): 触发验证模块提前复查。
    """
    fingerprint = str(alert.get("fingerprint") or "")
    now = datetime.now(timezone.utc)
    stmt = (
        select(FaultEvent)
        .filter_by(fingerprint=fingerprint)
        .where(FaultEvent.status.in_(ACTIVE_STATUSES))
    )
    rows = list((await db.execute(stmt)).scalars().all())
    if not rows:
        counts["ignored"] += 1
        return
    ev = min(rows, key=lambda item: item.detected_at)
    ev.status = "resolved"
    ev.resolved_at = now
    ev.mttr_seconds = max(0, int((now - ev.detected_at).total_seconds()))
    ev.updated_at = now
    await db.commit()
    counts["merged"] += 1


@router.post("/alerts", summary="接收 Alertmanager 标准 payload")
async def receive_alerts(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="payload 不是合法 JSON")

    counts = {"received": 0, "created": 0, "merged": 0, "ignored": 0}
    for alert in payload.get("alerts", []):
        counts["received"] += 1
        try:
            # 逐条 status（批量 payload 可能 firing/resolved 混合）
            if alert.get("status") == "resolved":
                await _handle_resolved(db, alert, counts)
            else:
                await _handle_firing(db, alert, counts)
        except Exception:
            logger.exception("告警处理失败 fingerprint=%s", alert.get("fingerprint"))

    return {k: counts[k] for k in ("received", "created", "merged", "ignored")}
