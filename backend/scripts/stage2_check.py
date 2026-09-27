"""阶段二验收脚本（docs/phase2-plan.md §4）：走 API 全链路（需服务器环境）。

用法（服务器 compose 内，backend 需已启动且配置 LLM_API_KEY）：
    docker compose exec backend python scripts/stage2_check.py

流程：登录 → 找 demo 的 payment-service Pod → POST 合成 OOM 告警到 webhook
→ 轮询至 awaiting_approval（≤180s）→ 校验 RCA（证据≥2 条跨 2 源）与提案
→ 二次 diagnose 断言 409 → approve 断言 fault_event_status=remediating。

注意：approve 会真实放行提案（阶段二停在 remediating，阶段三才执行修复）。
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
POLL_INTERVAL = 5


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


def main() -> int:
    ok = True
    pod_name = asyncio.run(_pick_pod())
    fingerprint = f"stage2check-{int(time.time())}"
    print(f"[stage2] 验收目标 pod={pod_name} fingerprint={fingerprint}")

    with _c() as c:
        # 1) 登录
        r = c.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": settings.admin_initial_password},
        )
        r.raise_for_status()
        token = r.json()["access_token"]
        c.headers["Authorization"] = f"Bearer {token}"
        print("[stage2] 登录 OK")

        # 2) 记录注入前活跃事件 id
        before = {item["id"] for item in c.get("/api/v1/faults", params={"status": "active"}).json()["items"]}

        # 3) 注入合成告警（webhook 无鉴权）
        alert = {
            "status": "firing",
            "labels": {
                "alertname": "PodOOMKilled",
                "severity": "critical",
                "namespace": settings.demo_namespace,
                "pod": pod_name,
                "container": "app",
            },
            "annotations": {"summary": "stage2 验收注入"},
            "startsAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "fingerprint": fingerprint,
        }
        r = c.post("/api/v1/webhooks/alerts", json={"alerts": [alert]})
        r.raise_for_status()
        print(f"[stage2] webhook 返回 {r.json()}")

        # 4) 轮询至 awaiting_approval
        deadline = time.time() + POLL_TIMEOUT
        detail = None
        last_status = "?"
        while time.time() < deadline:
            items = c.get("/api/v1/faults", params={"status": "active"}).json()["items"]
            for item in items:
                if item["id"] in before:
                    continue
                d = c.get(f"/api/v1/faults/{item['id']}").json()
                if d.get("fingerprint") == fingerprint:
                    detail = d
                    break
            if detail is not None:
                last_status = detail["status"]
                if last_status == "awaiting_approval":
                    break
            time.sleep(POLL_INTERVAL)
        if detail is None or detail["status"] != "awaiting_approval":
            print(f"[stage2] FAIL：{POLL_TIMEOUT}s 内未到 awaiting_approval（最后状态 {last_status}）")
            return 1
        print(f"[stage2] 状态流转 OK：→ awaiting_approval（fault_event_id={detail['id']}）")

        # 5) 校验 RCA 与提案
        diag = detail["diagnosis"]
        rem_list = detail["remediations"]
        sources = {e["source"] for e in (diag["evidence"] or [])}
        print(f"[stage2] RCA：fault_type={diag['fault_type']} confidence={diag['confidence']} "
              f"iterations={diag['iterations']} evidence={len(diag['evidence'])} 条/{len(sources)} 源")
        print(f"[stage2] RCA root_cause：{diag['root_cause']}")
        if len(diag["evidence"]) < 2 or len(sources) < 2:
            ok = False
            print("[stage2] FAIL：RCA 证据不满足 ≥2 条跨 2 源")
        if not rem_list:
            ok = False
            print("[stage2] FAIL：没有 remediation 提案")
        rem = rem_list[0] if rem_list else None
        if rem is not None:
            print(f"[stage2] 提案：{rem['action']} {rem['target']} params={rem['params']} "
                  f"risk={rem['risk_level']} policy={rem['policy']} status={rem['status']}")
            if rem["status"] != "pending" or rem["policy"] == "forbidden":
                ok = False
                print("[stage2] FAIL：提案状态/策略异常")

        # 6) 二次 diagnose → 409
        r = c.post(f"/api/v1/faults/{detail['id']}/diagnose")
        if r.status_code == 409:
            print(f"[stage2] 二次 diagnose 409 OK：{r.json()['detail']}")
        else:
            ok = False
            print(f"[stage2] FAIL：二次 diagnose 应 409，实际 {r.status_code} {r.text[:120]}")

        # 7) approve → remediating
        r = c.post(
            f"/api/v1/remediations/{rem['id']}/approve",
            json={"comment": "stage2 验收自动批准"},
        )
        if r.status_code == 200 and r.json().get("fault_event_status") == "remediating":
            print(f"[stage2] approve OK：{r.json()}")
        else:
            ok = False
            print(f"[stage2] FAIL：approve 异常 {r.status_code} {r.text[:200]}")

    print(f"[stage2] 结果：{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
