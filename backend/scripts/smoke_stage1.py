"""阶段一本地冒烟测试（无 K8s/Prometheus/Loki/DB 依赖，Python 3.11+ 运行）。

用法（仓库任意位置）：
    python backend/scripts/smoke_stage1.py

覆盖：模块导入、OpenAI 工具规格、decide()、_extract_json 容错、修复参数
schema、AgentState reducer 接线（含 MemorySaver 迷你图）、白名单幅度逻辑、
delete_pod 白名单边界、quantity 解析。服务器端到端验收另见 stage1_check.py。
"""
import operator
import os
import sys
import typing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.llm as L  # noqa: E402
import agent.schemas as S  # noqa: E402
import agent.state as ST  # noqa: E402
import agent.tools as T  # noqa: E402
import risk_control as RC  # noqa: E402
import risk_control.audit as A  # noqa: E402
import risk_control.whitelist as W  # noqa: E402

# 1) OpenAI 工具规格
specs = T.to_openai_specs()
names = [s["function"]["name"] for s in specs]
expected = [
    "get_pod_status", "get_pod_logs", "get_k8s_events", "get_metrics",
    "get_deployment", "get_node_status", "describe_pod", "get_service_status",
]
assert names == expected, names
assert specs[0]["function"]["parameters"]["required"] == ["namespace", "name"]
print(f"[1] to_openai_specs OK：8 个工具（{names[0]} ... {names[-1]}）")

# 2) decide()
d = RC.decide("restart_deployment")
assert d.risk_level == RC.RiskLevel.MEDIUM and d.policy == RC.Policy.REQUIRE_APPROVAL
assert d.risk_level.value == "medium" and d.policy.value == "require_approval"
dp = RC.decide("delete_pod")
assert dp.risk_level == RC.RiskLevel.HIGH and dp.policy == RC.Policy.REQUIRE_APPROVAL
assert RC.decide("delete_node").policy == RC.Policy.FORBIDDEN
assert RC.decide("rm_rf_slash").risk_level == RC.RiskLevel.CRITICAL
print("[2] decide() OK：已注册 / delete_pod=HIGH / delete_node / 未注册默认拒绝")

# 3) _extract_json 容错
f = L._extract_json
assert f('{"a": 1}') == {"a": 1}
assert f('```json\n{"a": 1}\n```') == {"a": 1}
assert f('result: {"a": {"b": 2}} end') == {"a": {"b": 2}}
print("[3] _extract_json OK：裸 JSON / 代码围栏 / 前后缀文本")

# 4) 修复工具参数模型
try:
    T.ScaleDeploymentParams.model_validate({})
    raise AssertionError("缺 replicas 应校验失败")
except Exception:
    pass
r = T.UpdateResourceLimitParams.model_validate({"container": "app", "memory_limit": "1Gi"})
assert r.cpu_limit is None
assert T.RestartDeploymentParams().model_dump() == {}
assert T.DeletePodParams().model_dump() == {}
print("[4] REMEDIATION_PARAM_MODELS OK（含 DeletePodParams）")

# 5) Evidence / RCAReport / RemediationPlan 与 DB CHECK 对齐
ev = S.Evidence(source="loki", tool="get_pod_logs", summary="x", data={"lines": []})
S.RCAReport(fault_type="OOM", root_cause="r", evidence=[ev], confidence=0.9,
            blast_radius="b", suggestion="s")
S.RemediationPlan(action="update_resource_limit", namespace="demo", target="p",
                  params={"container": "app", "memory_limit": "2Gi"}, reason="why")
S.RemediationPlan(action="delete_pod", namespace="demo", target="stress-cpu-13",
                  params={}, reason="压力 Pod 占满 CPU，删除止血")
try:
    S.RemediationPlan(action="delete_node", namespace="demo", target="n1", params={}, reason="x")
    raise AssertionError("未注册动作应被 Literal 拒绝")
except Exception:
    pass
print("[5] schemas OK（delete_pod 进 Literal、未注册值被拒）")

# 6) AgentState reducer 接线（py3.14 PEP 649 下 get_type_hints 对 TypedDict 有坑，
# 直接读 __annotations__；真实接线由第 7 项 langgraph 建图证明）
anns = ST.AgentState.__annotations__
meta = anns["evidence"]
assert typing.get_origin(meta) is typing.Annotated
assert meta.__metadata__[0] is operator.add
print("[6] AgentState OK：evidence reducer = operator.add")

# 7) 迷你 StateGraph + MemorySaver（阶段二 interrupt/checkpointer 风险点探路）
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402


def node_collect(state):
    return {
        "evidence": [S.Evidence(source="k8s_api", tool="t", summary="s", data={})],
        "iteration": state["iteration"] + 1,
    }


g = StateGraph(ST.AgentState)
g.add_node("collect", node_collect)
g.add_edge(START, "collect")
g.add_edge("collect", END)
app = g.compile(checkpointer=MemorySaver())
init = {
    "fault_event_id": 1, "alert": {}, "evidence": [], "hypotheses": [],
    "rca": None, "plan": None, "risk_decision": None, "iteration": 0,
    "rediagnose_count": 0,
}
out = app.invoke(init, config={"configurable": {"thread_id": "t1"}})
assert out["iteration"] == 1 and len(out["evidence"]) == 1, out
snap = app.get_state(config={"configurable": {"thread_id": "t1"}})
assert snap.values["iteration"] == 1
print("[7] StateGraph+MemorySaver OK：evidence 追加合并、checkpoint 可读")

# 8) 白名单调整幅度逻辑（纯函数）
from agent.tools import UpdateResourceLimitParams  # noqa: E402
from monitoring.models import DeploymentInfo, PodInfo  # noqa: E402

dep = DeploymentInfo(
    namespace="demo", name="p", replicas=2, ready_replicas=2, image="i",
    cpu_limit="500m", memory_limit="512Mi", labels={"kairos.io/managed": "true"},
)
assert W._limit_ratio_violations(UpdateResourceLimitParams(container="app", memory_limit="5Gi"), dep), "10x 必须拒绝"
assert W._limit_ratio_violations(UpdateResourceLimitParams(container="app", memory_limit="1Gi"), dep) == [], "2x 放行"
assert W._limit_ratio_violations(UpdateResourceLimitParams(container="app", memory_limit="128Mi"), dep), "0.25x 必须拒绝"
assert W._limit_ratio_violations(UpdateResourceLimitParams(container="app", memory_limit="256Mi"), dep) == [], "0.5x 边界放行"
assert W._limit_ratio_violations(UpdateResourceLimitParams(container="app"), dep), "缺参必须拒绝"
print("[8] whitelist 幅度逻辑 OK：10x / 2x / 0.25x / 0.5x 边界 / 缺参")

# 9) quantity 解析 + 资源标识
from monitoring.k8s import parse_quantity  # noqa: E402

assert parse_quantity("512Mi") == 512 * 1024 * 1024
assert parse_quantity("500m") == 0.5
assert A.resource_for("deployment", "demo", "payment-service") == "deployment/demo/payment-service"
print("[9] parse_quantity / resource_for OK")

# 10) delete_pod 白名单边界（bare-pod-only 纯函数，Phase 4 核心安全防线）
stress_pod = PodInfo(
    namespace="demo", name="stress-cpu-13", workload=None, node="n1",
    phase="Running", status="Running", ready=True, restarts=0, age_seconds=60,
    memory_limit_bytes=128 * 1024 * 1024,
)
biz_pod = PodInfo(
    namespace="demo", name="payment-service-7f9d8b", workload="payment-service", node="n1",
    phase="Running", status="Running", ready=True, restarts=1, age_seconds=3600,
    memory_limit_bytes=512 * 1024 * 1024,
)
assert W._delete_pod_violations(stress_pod) == [], "独立 Pod（无 owner）必须放行"
assert W._delete_pod_violations(biz_pod), "受管工作负载 Pod 必须拒绝"
assert W._delete_pod_violations(None), "目标 Pod 不存在必须拒绝"
print("[10] delete_pod 白名单边界 OK：独立 Pod 放行 / 业务 Pod 拒绝 / 不存在拒绝")

print("ALL SMOKE TESTS PASSED")
