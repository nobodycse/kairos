"""Alertmanager 告警接收（design.md §3.13，无鉴权，仅内网可达）。

Phase 0 只解析计数；Phase 1 实现去重 / 归并 / fault_events 落库
（design.md §5.3：SET alerts:{fingerprint} NX EX 3600 等）。
"""
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/alerts", summary="接收 Alertmanager 标准 payload")
async def receive_alerts(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="payload 不是合法 JSON")
    alerts = payload.get("alerts", [])
    # stub：全部计为 created；去重（ignored）与归并（merged）Phase 1 实现
    return {
        "received": len(alerts),
        "created": len(alerts),
        "merged": 0,
        "ignored": 0,
    }
