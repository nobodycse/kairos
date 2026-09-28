"""Agent 事件发射（design.md §4 / architecture.md §6.1）——Redis pub/sub + steps 窗口。

发布封格式（落地决策 §一.6）：`{"event": <类型>, "data": <§4 payload>}`，
channel = `sse:fault:{id}`，由 faults.py 的 stream 订阅转发为 SSE 帧。
- agent_step 同时进 `event:{id}:steps` 窗口（LPUSH+LTRIM 保留 20 条，EXPIRE 86400）；
- status_changed 同步 `event:{id}:status`（断线重连补发的状态源，§8）；
- verification_progress 只广播，不入 steps 窗口（recent_steps 仅收 agent_step，§4.2）。
所有事件为 best-effort：Redis 异常只记日志，不阻断诊断主流程。
"""
import json
import logging
from datetime import datetime, timezone

from core.redis import r as redis

logger = logging.getLogger(__name__)

_STEPS_LIMIT = 20  # §4.2：recent_steps 最多 20 条
_STEPS_TTL = 86400


def _now_iso() -> str:
    """ISO8601 Z 字符串（与 mock.py 的 SSE 契约一致）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _steps_key(fault_event_id: int) -> str:
    return f"event:{fault_event_id}:steps"


def _channel(fault_event_id: int) -> str:
    return f"sse:fault:{fault_event_id}"


async def _publish(fault_event_id: int, event: str, frame: dict) -> None:
    try:
        await redis.publish(
            _channel(fault_event_id),
            json.dumps({"event": event, "data": frame}, ensure_ascii=False),
        )
    except Exception:
        logger.exception("SSE publish 失败（best-effort 忽略）")


async def emit_agent_step(fault_event_id: int, payload: dict) -> None:
    """发 agent_step 事件（§4.4）。iteration/phase/step 与阶段字段由调用方给，at 在此补齐。"""
    frame = {"fault_event_id": fault_event_id, **payload, "at": _now_iso()}
    try:
        key = _steps_key(fault_event_id)
        await redis.lpush(key, json.dumps(frame, ensure_ascii=False))
        await redis.ltrim(key, 0, _STEPS_LIMIT - 1)
        await redis.expire(key, _STEPS_TTL)
    except Exception:
        logger.exception("steps 窗口写入失败（best-effort 忽略）")
    await _publish(fault_event_id, "agent_step", frame)


async def emit_status_changed(fault_event_id: int, frm: str, to: str, reason: str) -> None:
    """发 status_changed 事件（§4.3，字段名 from/to），并同步 event:{id}:status。"""
    frame = {
        "fault_event_id": fault_event_id,
        "from": frm,
        "to": to,
        "reason": reason,
        "at": _now_iso(),
    }
    try:
        await redis.set(f"event:{fault_event_id}:status", to)
    except Exception:
        logger.exception("status key 写入失败（best-effort 忽略）")
    await _publish(fault_event_id, "status_changed", frame)


async def emit_verification_progress(fault_event_id: int, payload: dict) -> None:
    """发 verification_progress 事件（§4.5）。只广播，不入 steps 窗口。"""
    frame = {"fault_event_id": fault_event_id, **payload, "at": _now_iso()}
    logger.info("verification_progress %s", json.dumps(frame, ensure_ascii=False))
    await _publish(fault_event_id, "verification_progress", frame)
