"""验证模块（architecture.md §6.7）：修复后观察窗口 3 分钟 / 30s 采样 / 6 个采样点。

判定口径（落地决策 §一.2）：任一采样点任一检查不通过 → 立即返回 False
（进入 rolling_back）；6 点全过 → True（resolved）。error_rate 的"呈下降趋势"
不做（MVP 只做阈值 <0.05，文档注记）。Prometheus/Loki 软依赖：拿不到数据
视为通过（与 cluster 路由/工具层的软依赖语义一致）。
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from agent import events
from agent.tools import _GLOBAL_EXPRS  # 与工具层同一 PromQL 口径（rules.yml 阈值）
from monitoring import clients

logger = logging.getLogger(__name__)

SAMPLES = 6
INTERVAL_SECONDS = 30
ERROR_RATE_THRESHOLD = 0.05  # rules.yml HTTPErrorRateHigh
P95_THRESHOLD = 2.0  # rules.yml HTTPLatencyHigh（秒）
_LOG_PATTERN = "(?i)(fatal|panic|out of memory|oomkilled)"  # §6.7：无新增 fatal/panic/OOM 关键字


async def _workload_pods(namespace: str, target: str | None) -> list:
    if not target:
        return []
    pods = await clients.k8s.list_pods(namespace)
    return [p for p in pods if p.workload == target]


async def _prom_value(expr: str) -> float | None:
    try:
        samples = await clients.prometheus.query(expr)
        return samples[0].value if samples else None
    except Exception:
        return None  # Prometheus 软依赖


async def _logs_clean(namespace: str, target: str | None) -> bool:
    """近 60s 目标 Pod 日志无 fatal/panic/OOM 关键字（§6.7）。"""
    if not target:
        return True
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=60)
    logql = f'{{namespace="{namespace}", pod=~"{re.escape(target)}.*"}} |~ "{_LOG_PATTERN}"'
    try:
        lines = await clients.loki.query_range(logql, start, end, limit=1)
        return not lines
    except Exception:
        return True  # Loki 软依赖


async def _sample_checks(
    namespace: str, target: str | None, baseline_restarts: int
) -> dict:
    """单个采样点的五项检查（§6.7 表）。

    target 为空（聚合类告警无 workload 维度）时 pod 维度检查直接通过。
    no_restarts 取 `<= 基线`：pod 因滚动更新被替换时计数总和可能回落，
    严格相等会误判（落地决策注记）。
    """
    pods = await _workload_pods(namespace, target)
    pod_ready = all(p.ready and p.phase == "Running" for p in pods) if pods else True
    restarts = sum(p.restarts for p in pods)
    no_restarts = restarts <= baseline_restarts if pods else True
    error_rate = await _prom_value(_GLOBAL_EXPRS["error_rate"])
    p95 = await _prom_value(_GLOBAL_EXPRS["p95_latency"])
    return {
        "pod_ready": pod_ready,
        "no_restarts": no_restarts,
        "error_rate": {
            "value": error_rate,
            "ok": error_rate is None or error_rate < ERROR_RATE_THRESHOLD,
        },
        "p95_latency": {"value": p95, "ok": p95 is None or p95 < P95_THRESHOLD},
        "logs_clean": await _logs_clean(namespace, target),
    }


def _all_passed(checks: dict) -> bool:
    return (
        checks["pod_ready"]
        and checks["no_restarts"]
        and checks["error_rate"]["ok"]
        and checks["p95_latency"]["ok"]
        and checks["logs_clean"]
    )


async def run_window(fault_event_id: int, namespace: str, target: str | None) -> bool:
    """观察窗口：6 个采样点 × 30s（t=30..180，给修复生效时间）。

    每个采样点发 verification_progress 事件（§4.5：sample/of/checks/all_passed）；
    任一不通过立即返回 False。
    """
    pods = await _workload_pods(namespace, target)
    baseline = sum(p.restarts for p in pods)
    for sample in range(1, SAMPLES + 1):
        await asyncio.sleep(INTERVAL_SECONDS)
        checks = await _sample_checks(namespace, target, baseline)
        passed = _all_passed(checks)
        await events.emit_verification_progress(
            fault_event_id,
            {"sample": sample, "of": SAMPLES, "checks": checks, "all_passed": passed},
        )
        logger.info(
            "verification sample=%s/%s all_passed=%s checks=%s",
            sample, SAMPLES, passed, checks,
        )
        if not passed:
            return False
    return True
