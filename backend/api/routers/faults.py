"""故障事件：列表 / 详情 / 触发诊断 / SSE 流 / 人工确认（design.md §3.5–§3.8、§4）。

Phase 0 返回假数据 + SSE 演示序列；Phase 2 接 LangGraph 与 Redis pub/sub。
"""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from api import mock
from api.deps import require_user
from core.security import decode_access_token

router = APIRouter(tags=["faults"])

# 终态：不允许再触发诊断（design.md §3.7）
_TERMINAL_STATES = {"resolved", "failed", "closed"}


@router.get("/faults", summary="故障事件列表（status 支持枚举值或 active，namespace 过滤）")
def list_faults(
    status: str | None = None,
    namespace: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _: str = Depends(require_user),
):
    items = mock.list_faults(status, namespace)
    start = (page - 1) * page_size
    return {
        "items": items[start : start + page_size],
        "total": len(items),
        "page": page,
        "page_size": page_size,
    }


@router.get("/faults/{fault_id}", summary="事件详情（诊断中时 diagnosis 为 null）")
def get_fault(fault_id: int, _: str = Depends(require_user)):
    fault = mock.FAULTS.get(fault_id)
    if fault is None:
        raise HTTPException(status_code=404, detail="故障事件不存在")
    return fault


@router.post("/faults/{fault_id}/diagnose", status_code=202, summary="手动（重新）触发诊断")
def diagnose(fault_id: int, _: str = Depends(require_user)):
    fault = mock.FAULTS.get(fault_id)
    if fault is None:
        raise HTTPException(status_code=404, detail="故障事件不存在")
    if fault["status"] == "diagnosing":
        raise HTTPException(status_code=409, detail="该事件正在诊断中")
    if fault["status"] in _TERMINAL_STATES:
        raise HTTPException(status_code=409, detail="该事件已结束")
    # Phase 2 在此 asyncio.create_task(agent_runner.run(fault_id))（design.md §5.1）
    return {"fault_event_id": fault_id, "status": "diagnosing"}


@router.post("/remediations/{remediation_id}/approve", summary="人工批准修复")
def approve(remediation_id: int, _: str = Depends(require_user)):
    remediation = mock.FAULTS[42]["remediations"][0]
    if remediation_id != remediation["id"]:
        raise HTTPException(status_code=404, detail="修复动作不存在")
    if remediation["status"] != "pending":
        raise HTTPException(status_code=409, detail="该动作当前状态不允许此操作")
    # stub：同步更新内存状态（二次调用可测 409）；Phase 2 在此以
    # Command(resume={"approved": True}) 恢复挂起的 graph（design.md §5.2）
    remediation["status"] = "approved"
    mock.FAULTS[42]["status"] = "remediating"
    return {
        "id": remediation_id,
        "status": "approved",
        "fault_event_status": "remediating",
    }


@router.post("/remediations/{remediation_id}/reject", summary="人工拒绝修复")
def reject(remediation_id: int, _: str = Depends(require_user)):
    remediation = mock.FAULTS[42]["remediations"][0]
    if remediation_id != remediation["id"]:
        raise HTTPException(status_code=404, detail="修复动作不存在")
    if remediation["status"] != "pending":
        raise HTTPException(status_code=409, detail="该动作当前状态不允许此操作")
    remediation["status"] = "rejected"
    mock.FAULTS[42]["status"] = "closed"
    return {
        "id": remediation_id,
        "status": "rejected",
        "fault_event_status": "closed",
    }


def _sse_frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get(
    "/faults/{fault_id}/stream",
    summary="SSE 事件流（契约见 design.md §4，/docs 覆盖不了 SSE）",
    response_class=StreamingResponse,
)
async def stream(fault_id: int, token: str = Query(...)):
    # EventSource 不能设置请求头，JWT 走 query 参数（design.md §4.1）
    if decode_access_token(token) is None:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    if fault_id not in mock.FAULTS:
        raise HTTPException(status_code=404, detail="故障事件不存在")

    async def event_stream():
        # 连接/重连先发 snapshot（design.md §4.2）
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
