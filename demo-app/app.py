"""KAIROS demo-app：被监控的示例微服务（契约见 docs/design.md §6）。

- 业务指标与告警规则 PromQL 严格一致（architecture.md §4.1）：
  demo_http_requests_total / demo_http_request_duration_seconds /
  demo_http_requests_in_flight
- 故障注入端点 /internal/*：注入状态存进程内存，Pod 重启即自动清零
  —— 这正是 OOM 修复后行为恢复正常的机制。
"""
import os
import random
import threading
import time

from fastapi import FastAPI, HTTPException, Query
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

app = FastAPI(title="kairos demo-app", docs_url=None, redoc_url=None)

# prometheus_client 会为 Counter 自动追加 _total 后缀，实际暴露名为
# demo_http_requests_total（与告警 PromQL 一致）
REQUESTS = Counter(
    "demo_http_requests",
    "demo HTTP 请求总数",
    ["status", "route"],
)
LATENCY = Histogram(
    "demo_http_request_duration_seconds",
    "demo HTTP 请求延迟（秒）",
    ["status", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
IN_FLIGHT = Gauge("demo_http_requests_in_flight", "demo 当前并发请求数")

_state_lock = threading.Lock()
_state = {"slow_ms": 0, "error_rate": 0.0}
# consume-memory 分配的持有引用，防止被 GC 回收
_memory_holders: list[bytearray] = []


@app.get("/pay")
def pay():
    """主路由：正常时随机 sleep 10–50ms 返回 200（design.md §6.1）。"""
    with IN_FLIGHT.track_inprogress():
        with _state_lock:
            slow_ms, error_rate = _state["slow_ms"], _state["error_rate"]
        latency = (random.uniform(10, 50) + slow_ms) / 1000
        time.sleep(latency)
        failed = error_rate > 0 and random.random() < error_rate
        status = "500" if failed else "200"
        REQUESTS.labels(status=status, route="/pay").inc()
        LATENCY.labels(status=status, route="/pay").observe(latency)
        if failed:
            raise HTTPException(status_code=500, detail="injected error")
        return {"ok": True, "latency_ms": round(latency * 1000)}


# ---------- 故障注入端点（design.md §6.2，无需 K8s 权限） ----------


@app.post("/internal/crash")
def crash():
    """立即退出进程；配合重启策略形成 CrashLoopBackOff。"""
    os._exit(1)


@app.post("/internal/consume-memory")
def consume_memory(bytes: int = Query(..., gt=0, le=2 * 1024**3)):
    """分配并持有 bytes 内存（触发 OOMKilled）。参数名按契约固定为 bytes。"""
    _memory_holders.append(bytearray(bytes))
    return {"holding_bytes": bytes}


@app.post("/internal/slow")
def slow(ms: int = Query(..., ge=0, le=60000)):
    """令 /pay 额外延迟 ms 毫秒（配合延迟告警）。"""
    with _state_lock:
        _state["slow_ms"] = ms
    return {"slow_ms": ms}


@app.post("/internal/errors")
def errors(rate: float = Query(..., ge=0, le=1)):
    """令 /pay 以 rate 概率返回 500（配合错误率告警）。"""
    with _state_lock:
        _state["error_rate"] = rate
    return {"error_rate": rate}


@app.post("/internal/reset")
def reset():
    """清除所有注入状态（实验收尾）。"""
    with _state_lock:
        _state.update(slow_ms=0, error_rate=0.0)
    _memory_holders.clear()
    return {"status": "reset"}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


app.mount("/metrics", make_asgi_app())
