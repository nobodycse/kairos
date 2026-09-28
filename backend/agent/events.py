"""Agent 内部事件发射接缝（design.md §4 / architecture.md §6.1）。

阶段二只发结构化日志（真实 SSE 链路属阶段三）：graph/runner 只依赖本模块，
阶段三把实现替换为 Redis publish `sse:fault:{id}` + `event:{id}:steps`
（20 条窗口），调用方无需改动。事件字段与 design.md §4 契约一致：
agent_step = {fault_event_id, iteration, phase, step, tool?, args?/duration_ms?,
evidence_summary?, at}；status_changed = {fault_event_id, from, to, reason, at}。
"""
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """ISO8601 Z 字符串（与 mock.py 的 SSE 契约一致）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


async def emit_agent_step(fault_event_id: int, payload: dict) -> None:
    """发 agent_step 事件。iteration/phase/step 与阶段字段由调用方给，at 在此补齐。"""
    frame = {"fault_event_id": fault_event_id, **payload, "at": _now_iso()}
    logger.info("agent_step %s", json.dumps(frame, ensure_ascii=False))


async def emit_status_changed(fault_event_id: int, frm: str, to: str, reason: str) -> None:
    """发 status_changed 事件（§4.3，字段名 from/to）。"""
    frame = {
        "fault_event_id": fault_event_id,
        "from": frm,
        "to": to,
        "reason": reason,
        "at": _now_iso(),
    }
    logger.info("status_changed %s", json.dumps(frame, ensure_ascii=False))


async def emit_verification_progress(fault_event_id: int, payload: dict) -> None:
    """发 verification_progress 事件（§4.5）。

    只走广播流，不入 steps 窗口（recent_steps 仅收 agent_step，§4.2）。
    """
    frame = {"fault_event_id": fault_event_id, **payload, "at": _now_iso()}
    logger.info("verification_progress %s", json.dumps(frame, ensure_ascii=False))
