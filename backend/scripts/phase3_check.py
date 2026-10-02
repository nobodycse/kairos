"""Phase 3 验收脚本（docs/phase3-plan.md §6）：三类故障实验全链路（需服务器）。

用法（服务器 compose 内，backend 需已启动）：
    docker compose exec backend python scripts/phase3_check.py
可选只跑某一类：
    docker compose exec backend python scripts/phase3_check.py oom
    docker compose exec backend python scripts/phase3_check.py pod_crash cpu_overload

流程（每类实验）：create → inject（202）→ 轮询关联事件 → 事件 awaiting_approval
时自动 approve（模拟运维确认）→ 轮询实验 finished → report 断言 →
收尾还原检查（oom limit 复原 / cpu_overload 压力 Pod 删除）→ 全部跑完断言
summary 与 compare。

断言口径（phase3-plan §2.4/§6）：detected 必须为 true（未检出=链路失败）；
diagnosed_correctly / auto_recovered 为实测质量指标——false 记 WARN 不判失败，
如实进评估表。预期时长：oom ~5 分钟、pod_crash ~8 分钟、cpu_overload ~12 分钟
（ContainerCPUHigh for 5m），轮询上限已放宽（§7 风险预案）。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from core.config import settings  # noqa: E402

BASE = "http://127.0.0.1:8000"
TARGET = "payment-service"
# 轮询上限（秒）：关联窗口 10m + 闭环余量；cpu_overload 按 §7 放宽到 45m
FINISHED_TIMEOUT = {"oom": 1200, "pod_crash": 1200, "cpu_overload": 2700}
EVENT_TIMEOUT = 1320  # 等关联事件出现（关联窗口 20 分钟 + 轮询余量）
POLL_INTERVAL = 15
SETTLE_SECONDS = 90  # 实验间沉淀：等上一实验收尾还原的滚动更新稳定后再注入

SUPPORTED = ("oom", "pod_crash", "cpu_overload")


def _c() -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=30)


def login(c: httpx.Client) -> None:
    r = c.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": settings.admin_initial_password},
    )
    r.raise_for_status()
    c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    print("[phase3] 登录 OK")


def _log(msg: str) -> None:
    print(f"[phase3 {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def poll(fn, timeout: int, desc: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value is not None:
            return value
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"等待超时（{timeout}s）：{desc}")


def deployment_limit(c: httpx.Client, ns: str, name: str) -> str | None:
    rows = c.get("/api/v1/cluster/deployments", params={"namespace": ns}).json()["items"]
    for row in rows:
        if row["name"] == name:
            return row.get("memory_limit")
    return None


def stress_pod_exists(c: httpx.Client, pod_name: str) -> bool:
    rows = c.get("/api/v1/cluster/pods", params={"namespace": "demo", "page_size": 100}).json()["items"]
    return any(p["name"] == pod_name for p in rows)


def approve_pending(c: httpx.Client, fault_event_id: int) -> bool:
    """事件 awaiting_approval 时批准全部 pending 提案（返回是否有动作）。

    只在 awaiting_approval 时批准：事件已终态（评估器收敛过）时批准无意义，
    还会在 faultlab 还原后撞执行器白名单（服务器首轮实测教训）。"""
    detail = c.get(f"/api/v1/faults/{fault_event_id}").json()
    if detail.get("status") != "awaiting_approval":
        return False
    acted = False
    for rem in detail.get("remediations") or []:
        if rem.get("status") == "pending":
            _log(f"事件 #{fault_event_id} 自动批准提案 #{rem['id']}（{rem['action']} {rem.get('params')}）")
            r = c.post(
                f"/api/v1/remediations/{rem['id']}/approve",
                json={"comment": "phase3_check 自动确认"},
            )
            if r.status_code != 200:
                _log(f"approve 返回 {r.status_code}：{r.text[:150]}（可能已被并发处理，继续）")
            acted = True
    return acted


def run_experiment(c: httpx.Client, fault_type: str) -> dict:
    _log(f"===== 实验 {fault_type} 开始 =====")
    limit_before = deployment_limit(c, "demo", TARGET)
    _log(f"注入前 {TARGET} memory_limit={limit_before}")

    # oom 用后端默认值（32Mi，服务器实测 64/48Mi 不触发 OOM）；其余类型无参数
    r = c.post("/api/v1/experiments", json={"fault_type": fault_type, "target_workload": TARGET})
    if r.status_code != 201:
        raise AssertionError(f"创建实验失败 {r.status_code}：{r.text[:200]}")
    exp = r.json()
    exp_id = exp["id"]
    _log(f"创建实验 #{exp_id} OK（201）")

    r = c.post(f"/api/v1/experiments/{exp_id}/inject")
    if r.status_code != 202:
        raise AssertionError(f"注入失败 {r.status_code}：{r.text[:200]}（409=已有进行中实验？）")
    _log(f"注入已受理（202）：{r.json()}")

    # 等关联事件（10 分钟窗口；cpu_overload 告警 for 5m 也在此上限内）
    deadline = time.monotonic() + EVENT_TIMEOUT
    fault_event_id = None
    while time.monotonic() < deadline:
        report = c.get(f"/api/v1/experiments/{exp_id}/report").json()
        if report.get("fault_event_id"):
            fault_event_id = report["fault_event_id"]
            break
        if report["status"] == "finished":
            break
        time.sleep(POLL_INTERVAL)
    if fault_event_id is None:
        report = c.get(f"/api/v1/experiments/{exp_id}/report").json()
        result = report.get("result") or {}
        raise AssertionError(
            f"实验 #{exp_id} 未关联到故障事件（detected={result.get('detected')}，"
            f"notes={result.get('notes')}）——告警链路或注入未生效"
        )
    _log(f"实验 #{exp_id} 关联事件 #{fault_event_id} OK")

    # 事件 awaiting_approval → approve；轮询实验 finished
    def finished():
        approve_pending(c, fault_event_id)
        report = c.get(f"/api/v1/experiments/{exp_id}/report").json()
        if report["status"] == "finished":
            return report
        return None

    report = poll(finished, FINISHED_TIMEOUT[fault_type], f"实验 #{exp_id} 闭环（finished）")
    result = report["result"]
    _log(
        f"实验 #{exp_id} finished：detected={result['detected']} "
        f"latency={result['detection_latency_s']}s diagnosed_correctly={result['diagnosed_correctly']} "
        f"auto_recovered={result['auto_recovered']} mttr={result['mttr_s']}s "
        f"false_action={result['false_action']}"
    )
    _log(f"notes：{result.get('notes')}")

    # 收尾还原检查
    if fault_type == "oom":
        limit_after = deployment_limit(c, "demo", TARGET)
        if limit_before and limit_after != limit_before:
            raise AssertionError(f"oom 还原失败：memory_limit {limit_before} → {limit_after}")
        _log(f"oom 还原检查 OK（memory_limit={limit_after}）")
    if fault_type == "cpu_overload":
        pod_name = f"stress-cpu-{exp_id}"
        # 删除是异步的：Pod 可能短暂处于 Terminating，轮询等待真正消失
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if not stress_pod_exists(c, pod_name):
                _log("cpu_overload 收尾检查 OK（压力 Pod 已删除）")
                break
            time.sleep(5)
        else:
            raise AssertionError(f"cpu_overload 收尾失败：压力 Pod {pod_name} 60s 内仍存在")

    _print_faultlab_audits(exp_id)
    return {"exp_id": exp_id, "fault_event_id": fault_event_id, "result": result}


_AUDIT_LOOP = None  # asyncpg 连接池绑定首个 loop，全程复用（重复 asyncio.run 会跨 loop 报错）


def _print_faultlab_audits(exp_id: int) -> None:
    """打印该实验的注入/还原审计（text()+绑定参数，Mimosa 误报绕行写法）。"""
    import asyncio

    from sqlalchemy import text

    from core.db import SessionLocal

    global _AUDIT_LOOP
    if _AUDIT_LOOP is None:
        _AUDIT_LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_AUDIT_LOOP)

    async def _q():
        async with SessionLocal() as db:
            rows = await db.execute(
                text(
                    "SELECT action, result, resource, created_at FROM audit_logs "
                    "WHERE action IN ('fault_inject', 'fault_restore') "
                    "AND (resource LIKE :pat OR resource = :res) "
                    "ORDER BY created_at DESC LIMIT 6"
                ),
                {"pat": "pod/%", "res": f"experiment/{exp_id}"},
            )
            return rows.all()

    try:
        for action, result, resource, _created in _AUDIT_LOOP.run_until_complete(_q()):
            _log(f"audit {action} {result} {resource}")
    except Exception as e:  # noqa: BLE001  审计打印失败不影响验收主流程
        _log(f"审计打印跳过：{e}")


def check_summary_and_compare(c: httpx.Client, runs: dict[str, dict]) -> bool:
    ok = True
    summary = c.get("/api/v1/reports/summary").json()
    _log(f"summary：total={summary['total_experiments']} overall={json.dumps(summary['overall'])}")
    _log(f"by_fault_type：{json.dumps(summary['by_fault_type'], ensure_ascii=False)}")
    if summary["total_experiments"] < len(runs):
        _log(f"FAIL：summary.total_experiments={summary['total_experiments']} < 实验数 {len(runs)}")
        ok = False
    types = {row["fault_type"] for row in summary["by_fault_type"]}
    for ft in runs:
        if ft not in types:
            _log(f"FAIL：summary.by_fault_type 缺少 {ft}")
            ok = False

    for ft, run in runs.items():
        if not run["result"].get("auto_recovered"):
            _log(f"[compare] 实验 #{run['exp_id']}（{ft}）事件未恢复，跳过对比断言")
            continue
        r = c.get(f"/api/v1/experiments/{run['exp_id']}/compare")
        if r.status_code != 200:
            _log(f"FAIL：compare {ft} 返回 {r.status_code}：{r.text[:150]}")
            ok = False
            continue
        data = r.json()
        keys = [m["key"] for m in data["metrics"]]
        n_fault = len(data["metrics"][0]["fault"]["points"])
        n_recov = len(data["metrics"][0]["recovery"]["points"])
        _log(
            f"[compare] 实验 #{run['exp_id']}（{ft}）OK：metrics={keys} "
            f"故障窗点数={n_fault} 恢复窗点数={n_recov}"
        )
        if set(keys) != {"error_rate", "p95_latency", "cpu_usage", "memory_usage"}:
            _log("FAIL：compare metrics 不完整")
            ok = False
    return ok


def main() -> int:
    targets = [a for a in sys.argv[1:] if a in SUPPORTED] or list(SUPPORTED)
    unknown = [a for a in sys.argv[1:] if a not in SUPPORTED]
    if unknown:
        print(f"不支持故障类型：{unknown}（当前支持 {SUPPORTED}）")
        return 2

    ok = True
    runs: dict[str, dict] = {}
    with _c() as c:
        login(c)
        for idx, ft in enumerate(targets):
            if idx > 0:
                _log(f"沉淀等待 {SETTLE_SECONDS}s（上一实验收尾还原的滚动更新稳定）")
                time.sleep(SETTLE_SECONDS)
            try:
                runs[ft] = run_experiment(c, ft)
            except Exception as e:  # noqa: BLE001  单类失败继续跑其余，最后统一退出码
                _log(f"FAIL：{ft} 实验链路异常：{e}")
                ok = False
        if runs:
            ok = check_summary_and_compare(c, runs) and ok

    _log("===== 评估表实测数据（experiments.md 用） =====")
    for ft, run in runs.items():
        res = run["result"]
        _log(
            f"{ft}: detected={res['detected']} latency={res['detection_latency_s']}s "
            f"diagnosed={res['diagnosed_correctly']} recovered={res['auto_recovered']} "
            f"mttr={res['mttr_s']}s false_action={res['false_action']}"
        )
    if ok:
        _log("PHASE3 CHECK PASSED")
        return 0
    _log("PHASE3 CHECK FAILED（详见上方 FAIL/WARN 行）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
