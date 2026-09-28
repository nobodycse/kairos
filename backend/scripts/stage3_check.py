"""阶段三验收脚本（docs/phase2-plan.md §5）：执行→验证→resolved 全链路（需服务器）。

用法（服务器 compose 内，backend 需已启动且配置 LLM_API_KEY）：
    docker compose exec backend python scripts/stage3_check.py

流程：登录 → 注入合成 OOM 告警 → 轮询至 awaiting_approval → approve
→ SSE 流上收 verification_progress（happy path 6 个采样点）与
status_changed→resolved → 校验详情（resolved_at/mttr/提案 succeeded+snapshot）
→ 打印执行前后审计。

持续故障（回滚路径）为手动场景——操作指引（另开终端）：
    1. kubectl -n demo set image deploy/payment-service app=nginx:1.99  # 不存在的 tag
    2. curl 注入 PodOOMKilled 告警（payload 同本脚本的 alert 构造）或用 diagnose 接口
    3. 预期：agent 提议 restart → 执行成功但 Pod 起不来 → 验证不过 → 自动回滚
       （快照恢复镜像/副本）→ 重诊断 2 次后事件 failed、audit 有 rollback 记录
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from core.config import settings  # noqa: E402
from monitoring import clients  # noqa: E402

BASE = "http://127.0.0.1:8000"
POLL_TIMEOUT = 180
VERIFY_TIMEOUT = 300


def _c(token: str | None = None) -> httpx.Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=BASE, headers=headers, timeout=30)


async def _pick_pod() -> str:
    await clients.init_clients()
    pods = await clients.k8s.list_pods(settings.demo_namespace)
    for p in pods:
        if (p.workload and "payment" in p.workload) or p.name.startswith("payment"):
            return p.name
    if not pods:
        raise RuntimeError(f"{settings.demo_namespace} 命名空间没有 Pod")
    return pods[0].name


def _print_audits(fault_event_id: int) -> None:
    """打印执行前后的审计（text()+绑定参数，Mimosa 误报绕行写法）。"""
    from sqlalchemy import text

    from core.db import SessionLocal

    async def _q():
        async with SessionLocal() as db:
            rows = await db.execute(
                text(
                    "SELECT actor, action, result, detail, created_at FROM audit_logs "
                    "WHERE resource = :res AND action IN ('restart_deployment', "
                    "'update_resource_limit', 'scale_deployment', 'rollback_deployment') "
                    "ORDER BY created_at DESC LIMIT 8"
                ),
                {"res": f"deployment/{settings.demo_namespace}/payment-service"},
            )
            return rows.all()

    try:
        for actor, action, result, detail, created in asyncio.run(_q()):
            brief = {k: detail.get(k) for k in ("stage", "diff", "error") if detail and detail.get(k)}
            print(f"[stage3] audit {action} {result} by {actor}: {json.dumps(brief, ensure_ascii=False)[:200]}")
    except Exception as e:
        print(f"[stage3] 审计打印跳过（DB 不可达？）：{e}")


def main() -> int:
    ok = True
    pod_name = asyncio.run(_pick_pod())
    fingerprint = f"stage3check-{int(time.time())}"
    print(f"[stage3] 验收目标 pod={pod_name} fingerprint={fingerprint}")

    with _c() as c:
        # 1) 登录 + 记录注入前活跃事件
        r = c.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": settings.admin_initial_password},
        )
        r.raise_for_status()
        token = r.json()["access_token"]
        c.headers["Authorization"] = f"Bearer {token}"
        before = {i["id"] for i in c.get("/api/v1/faults", params={"status": "active"}).json()["items"]}

        # 2) 注入告警
        alert = {
            "status": "firing",
            "labels": {
                "alertname": "PodOOMKilled",
                "severity": "critical",
                "namespace": settings.demo_namespace,
                "pod": pod_name,
                "container": "app",
            },
            "annotations": {"summary": "stage3 验收注入"},
            "startsAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "fingerprint": fingerprint,
        }
        c.post("/api/v1/webhooks/alerts", json={"alerts": [alert]}).raise_for_status()
        print("[stage3] 告警已注入")

        # 3) 轮询至 awaiting_approval
        deadline = time.time() + POLL_TIMEOUT
        detail = None
        while time.time() < deadline:
            items = c.get("/api/v1/faults", params={"status": "active"}).json()["items"]
            for item in items:
                if item["id"] in before:
                    continue
                d = c.get(f"/api/v1/faults/{item['id']}").json()
                if d.get("fingerprint") == fingerprint:
                    detail = d
                    break
            if detail is not None and detail["status"] == "awaiting_approval":
                break
            time.sleep(5)
        if detail is None or detail["status"] != "awaiting_approval":
            print(f"[stage3] FAIL：未到 awaiting_approval（最后状态 {detail['status'] if detail else '?'}）")
            return 1
        fault_id = detail["id"]
        rem = (detail.get("remediations") or [None])[0]
        if rem is None or rem["status"] != "pending":
            print("[stage3] FAIL：无 pending 提案")
            return 1
        print(f"[stage3] awaiting_approval OK：提案 {rem['action']} {rem['target']} risk={rem['risk_level']}")

        # 4) approve，然后挂上 SSE 流收验证进度
        r = c.post(
            f"/api/v1/remediations/{rem['id']}/approve",
            json={"comment": "stage3 验收自动批准"},
        )
        if r.status_code != 200 or r.json().get("fault_event_status") != "remediating":
            print(f"[stage3] FAIL：approve 异常 {r.status_code} {r.text[:200]}")
            return 1
        print("[stage3] approve OK → remediating")

        progress, resolved_seen, last_status = [], False, "?"
        try:
            with c.stream(
                "GET",
                f"/api/v1/faults/{fault_id}/stream",
                params={"token": token},
                timeout=httpx.Timeout(VERIFY_TIMEOUT, read=VERIFY_TIMEOUT),
            ) as resp:
                current = None
                for line in resp.iter_lines():
                    if line.startswith("event:"):
                        current = line.split(":", 1)[1].strip()
                    elif line.startswith("data:") and current:
                        data = json.loads(line.split(":", 1)[1].strip())
                        if current == "verification_progress":
                            progress.append(data)
                            print(
                                f"[stage3] progress {data['sample']}/{data['of']} "
                                f"all_passed={data['all_passed']}"
                            )
                        if current == "status_changed":
                            last_status = data.get("to")
                            print(f"[stage3] status_changed → {last_status}（{data.get('reason')}）")
                            if last_status == "resolved":
                                resolved_seen = True
                                break
                            if last_status in ("failed", "rolling_back"):
                                break
        except httpx.ReadTimeout:
            print("[stage3] SSE 读取超时")
        print(f"[stage3] verification_progress 共 {len(progress)} 个采样点（happy path 应为 6）")

        # 5) 校验详情
        d = c.get(f"/api/v1/faults/{fault_id}").json()
        if not (d["status"] == "resolved" and d.get("resolved_at") and d.get("mttr_seconds") is not None):
            ok = False
            print(f"[stage3] FAIL：最终状态 {d['status']} resolved_at={d.get('resolved_at')} mttr={d.get('mttr_seconds')}")
        else:
            print(f"[stage3] resolved OK：mttr={d['mttr_seconds']}s resolved_at={d['resolved_at']}")
        rem_final = (d.get("remediations") or [{}])[0]
        if rem_final.get("status") != "succeeded" or not rem_final.get("snapshot") or not rem_final.get("executed_at"):
            ok = False
            print(f"[stage3] FAIL：提案终态异常 {rem_final.get('status')} snapshot={bool(rem_final.get('snapshot'))}")
        else:
            print(f"[stage3] 提案 OK：status={rem_final['status']} snapshot={rem_final['snapshot']} executed_at={rem_final['executed_at']}")
        if resolved_seen and len(progress) == 6:
            print("[stage3] SSE OK：6 采样点 + status_changed→resolved")
        else:
            ok = False
            print(f"[stage3] FAIL：SSE 断言（progress={len(progress)} resolved_seen={resolved_seen}）")

    _print_audits(detail["id"] if detail else 0)
    print(f"[stage3] 结果：{'PASS' if ok else 'FAIL'}")
    print("[stage3] 回滚路径为手动场景，操作指引见本文件 docstring。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
