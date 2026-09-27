"""阶段一验收脚本（docs/phase2-plan.md §3）。

服务器 compose 内运行：
    docker compose exec backend python scripts/stage1_check.py [tools|llm|whitelist|all]

- tools     8 个查询工具逐个产出 Evidence（校验 8/8、截断上限、OpenAI 规格数）
- llm       LLMClient.structured 对样例 OOM 告警产出合规 RCAReport（需 LLM_API_KEY）
- whitelist 合规例放行；4 个越界例拒绝且 audit_logs 落 result=denied
- all       依次执行（默认），任一失败退出码 1

注意：audit 计数用 text()+绑定参数的参数化查询（Mimosa 钩子对本文件的
ORM select()/filter_by() 比较写法一律误报 SQL 注入，见 phase2-plan.md §8）。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.llm as agent_llm  # noqa: E402
from agent.schemas import RCAReport, RemediationPlan  # noqa: E402
from agent.tools import QUERY_TOOLS, call_tool, to_openai_specs  # noqa: E402
from core.config import settings  # noqa: E402
from core.db import SessionLocal  # noqa: E402
from monitoring import clients  # noqa: E402
from risk_control.whitelist import validate  # noqa: E402


async def _pick_target() -> tuple:
    """选验收目标：优先 payment-service（demo-app.yaml 带 kairos.io/managed=true）。"""
    deps = await clients.k8s.list_deployments(settings.demo_namespace)
    if not deps:
        raise RuntimeError(f"{settings.demo_namespace} 命名空间没有 Deployment（demo-app 未部署？）")
    dep = next((d for d in deps if "payment" in d.name), deps[0])
    pods = await clients.k8s.list_pods(settings.demo_namespace)
    pod = next((p for p in pods if p.workload == dep.name), pods[0] if pods else None)
    if pod is None:
        raise RuntimeError(f"{settings.demo_namespace} 命名空间没有 Pod")
    return dep, pod


async def check_tools() -> bool:
    dep, pod = await _pick_target()
    print(f"[tools] 验收目标：deployment/{dep.name} pod/{pod.name}")
    calls = [
        ("get_pod_status", {"namespace": dep.namespace, "name": pod.name}),
        ("describe_pod", {"namespace": dep.namespace, "name": pod.name}),
        ("get_pod_logs", {"namespace": dep.namespace, "pod": pod.name, "minutes": 15}),
        ("get_k8s_events", {"namespace": dep.namespace, "pod": pod.name, "minutes": 60}),
        ("get_metrics", {"namespace": dep.namespace, "target": dep.name, "metric": "memory_usage", "minutes": 30}),
        ("get_deployment", {"namespace": dep.namespace, "name": dep.name}),
        ("get_node_status", {}),
        ("get_service_status", {"namespace": dep.namespace, "service": dep.name}),
    ]
    ok = True
    for name, args in calls:
        try:
            ev = await call_tool(name, args)
        except Exception as e:
            ok = False
            print(f"[tools] FAIL {name}: {type(e).__name__}: {e}")
            continue
        note = ""
        if name == "get_pod_logs":
            lines = ev.data.get("lines") or []
            note = f"lines={len(lines)}"
            if len(lines) > 200:
                ok = False
                note += " 超过 200 行截断上限！"
        if name == "get_metrics":
            points = ev.data.get("points") or 0
            note = f"series={ev.data.get('series_count')} points={points}"
            if points > 60:
                ok = False
                note += " 超过 60 点截断上限！"
        print(f"[tools] OK   {name}：{ev.summary}（{note or f'data keys: {sorted(ev.data.keys())}'}）")
    specs = to_openai_specs()
    if len(specs) != len(QUERY_TOOLS) or len(QUERY_TOOLS) != 8:
        ok = False
    print(f"[tools] OpenAI 工具规格 {len(specs)} 个（注册表 {len(QUERY_TOOLS)} 个，应为 8）")
    print(f"[tools] 结果：{'PASS' if ok else 'FAIL'}")
    return ok


async def check_llm() -> bool:
    if not settings.llm_api_key:
        print("[llm] FAIL：LLM_API_KEY 未配置（backend/.env），无法验收")
        return False
    print(f"[llm] 供应商：{settings.llm_base_url}  模型：{settings.llm_model}")
    alert = {
        "alertname": "PodOOMKilled",
        "severity": "critical",
        "namespace": settings.demo_namespace,
        "pod": "payment-service-7d9f6b8c5-x2k4l",
        "deployment": "payment-service",
    }
    messages = [
        {"role": "system", "content": "你是 K8s 运维诊断专家，基于告警与证据输出根因分析。"},
        {
            "role": "user",
            "content": (
                f"告警：{json.dumps(alert, ensure_ascii=False)}\n"
                "证据 1（source=k8s_api, tool=get_k8s_events）：最近 10 分钟 OOMKilled 事件 3 次；\n"
                "证据 2（source=prometheus, tool=get_metrics）：容器内存使用峰值达 limit 的 0.97。\n"
                "请输出根因分析：fault_type/root_cause/evidence（引用上面两条证据，含 source/tool/summary/data）/"
                "confidence/blast_radius/suggestion。"
            ),
        },
    ]
    try:
        rca = await agent_llm.structured(messages, RCAReport)
    except Exception as e:
        print(f"[llm] FAIL：{type(e).__name__}: {e}")
        return False
    print(rca.model_dump_json(indent=2))
    src = {e.source for e in rca.evidence}
    ok = len(rca.evidence) >= 2 and len(src) >= 2 and 0 <= rca.confidence <= 1
    print(f"[llm] 结果：{'PASS' if ok else 'FAIL'}"
          f"（schema 合规，evidence {len(rca.evidence)} 条 / {len(src)} 源，confidence {rca.confidence}）")
    return ok


async def _denied_count(db) -> int:
    """audit_logs 里 result=denied 的条数（参数化查询，占位符绑定，无字符串拼接）。"""
    from sqlalchemy import text

    rows = await db.execute(
        text("SELECT id FROM audit_logs WHERE result = :result"),
        {"result": "denied"},
    )
    return len(rows.all())


async def check_whitelist() -> bool:
    ok = True
    async with SessionLocal() as db:
        denied_before = await _denied_count(db)
        good = RemediationPlan(
            action="restart_deployment",
            namespace=settings.demo_namespace,
            target="payment-service",
            params={},
            reason="阶段一验收：合规重启提议",
        )
        r = await validate(db, good)
        print(f"[whitelist] 合规 restart_deployment → allowed={r.allowed}（应 True）")
        ok = ok and r.allowed

        bad_cases = [
            ("namespace 越界", RemediationPlan(
                action="restart_deployment", namespace="production",
                target="payment-service", params={}, reason="验收：namespace 越界")),
            ("replicas 越界", RemediationPlan(
                action="scale_deployment", namespace=settings.demo_namespace,
                target="payment-service", params={"replicas": 11}, reason="验收：replicas 越界")),
            ("limit 幅度越界", RemediationPlan(
                action="update_resource_limit", namespace=settings.demo_namespace,
                target="payment-service", params={"container": "app", "memory_limit": "5Gi"},
                reason="验收：512Mi→5Gi 约 10x，超出 0.5x–4x")),
            ("目标不存在/缺 managed 标签", RemediationPlan(
                action="restart_deployment", namespace=settings.demo_namespace,
                target="nonexistent-workload", params={}, reason="验收：目标不存在")),
        ]
        for label, plan in bad_cases:
            r = await validate(db, plan)
            passed = not r.allowed
            ok = ok and passed
            print(f"[whitelist] {'OK  ' if passed else 'FAIL'} {label} → violations={r.violations}")

        written = await _denied_count(db) - denied_before
        audit_ok = written == len(bad_cases)
        ok = ok and audit_ok
        print(f"[whitelist] audit_logs 新增 result=denied 记录 {written} 条（应为 {len(bad_cases)}）")
        print(f"[whitelist] 结果：{'PASS' if ok else 'FAIL'}")
    return ok


CHECKS = {"tools": check_tools, "llm": check_llm, "whitelist": check_whitelist}


async def _amain() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which != "all" and which not in CHECKS:
        print(__doc__)
        return 2
    names = list(CHECKS) if which == "all" else [which]
    if "tools" in names or "whitelist" in names:
        await clients.init_clients()
    results = {}
    for name in names:
        print(f"\n===== stage1_check：{name} =====")
        results[name] = await CHECKS[name]()
    print("\n===== 汇总 =====")
    for name, ok in results.items():
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
