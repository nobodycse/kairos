"""阶段二本地冒烟测试：假 LLM/假工具/假白名单走查诊断图（无集群/DB/LLM 依赖）。

用法：python backend/scripts/smoke_stage2.py

覆盖：
1. collect 循环：多轮工具调用、同工具+同参数去重保最新、iteration 计数
2. REQUIRE_APPROVAL：gate_check 决策入状态 → gate_wait interrupt 挂起
3. Command(resume={"approved": True/False}) 两分支恢复走完
4. 白名单拒绝：不经 interrupt 直接结束（runner 置 failed 的语义在此验证图部分）
5. AUTO 分支（桩 decide）：不挂起直接结束
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.disable(logging.CRITICAL)  # 冒烟静音：events 的 best-effort Redis 日志不关心

from contextlib import asynccontextmanager

import agent.graph as G  # noqa: E402
from agent.llm import LLMResponse, ToolCall  # noqa: E402
from agent.schemas import Evidence, RemediationPlan, RCAReport  # noqa: E402
from agent.tools import ToolError  # noqa: E402
from langgraph.types import Command  # noqa: E402
from remediation.executor import ExecResult  # noqa: E402
from risk_control import Decision, Policy, RiskLevel  # noqa: E402
from risk_control.whitelist import WhitelistResult  # noqa: E402

CANNED_EVIDENCE = [
    Evidence(source="k8s_api", tool="get_pod_status", summary="重启 3 次", data={"restarts": 3}),
    Evidence(source="events", tool="get_k8s_events", summary="OOMKilled x2", data={"reasons": ["OOMKilled"]}),
    Evidence(source="loki", tool="get_pod_logs", summary="out of memory", data={"lines": 5}),
]


class FakeLLM:
    """脚本化 LLM：chat 按轮次回放工具调用，structured 按 schema 类型回放。"""

    def __init__(self):
        self.chat_round = 0

    async def chat(self, messages, tools=None):
        self.chat_round += 1
        if self.chat_round == 1:
            return LLMResponse(
                content="先查 Pod 状态与事件",
                tool_calls=[
                    ToolCall(id="c1", name="get_pod_status", arguments={"namespace": "demo", "name": "p1"}),
                    ToolCall(id="c2", name="get_k8s_events", arguments={"namespace": "demo", "pod": "p1"}),
                ],
            )
        if self.chat_round == 2:
            # get_pod_status 同参重复（验证去重保最新）+ 补一条日志证据
            return LLMResponse(
                content="复查状态并看日志",
                tool_calls=[
                    ToolCall(id="c3", name="get_pod_status", arguments={"namespace": "demo", "name": "p1"}),
                    ToolCall(id="c4", name="get_pod_logs", arguments={"namespace": "demo", "pod": "p1"}),
                ],
            )
        return LLMResponse(content="证据足够：容器 OOMKilled 导致 CrashLoop。", tool_calls=[])

    async def structured(self, messages, schema):
        if schema is RCAReport:
            return RCAReport(
                fault_type="OOM", root_cause="内存超限被 OOMKill",
                evidence=CANNED_EVIDENCE[:2], confidence=0.9,
                blast_radius="demo/payment-service", suggestion="提高内存上限或重启",
            )
        if schema is RemediationPlan:
            return RemediationPlan(
                action="restart_deployment", namespace="demo",
                target="payment-service", params={}, reason="清除异常状态",
            )
        raise AssertionError(f"未预期的 schema：{schema}")


async def fake_call_tool(name, arguments):
    by_tool = {e.tool: e for e in CANNED_EVIDENCE}
    if name not in by_tool:
        raise ToolError(f"未知工具：{name}")
    return by_tool[name]


async def whitelist_ok(db, plan):
    return WhitelistResult(allowed=True, violations=[])


async def whitelist_deny(db, plan):
    return WhitelistResult(allowed=False, violations=["模拟越界"])


# ---------- 阶段三节点打桩（approve/AUTO 会走到 execute_fix/verify） ----------


class _FakeEv:
    def __init__(self):
        self.status = "detected"
        self.rediagnose_count = 0
        self.updated_at = None


_EV = _FakeEv()


class _FakeDB:
    async def get(self, model, key):
        return _EV

    async def commit(self):
        return None


@asynccontextmanager
async def _fake_session():
    yield _FakeDB()


async def _fake_execute_ok(db, fault_event_id, plan, decision):
    return ExecResult(success=True, message="stub 执行成功")


async def _fake_rollback_ok(db, fault_event_id, plan):
    return ExecResult(success=True, message="stub 回滚成功")


async def _fake_window_pass(fault_event_id, namespace, target):
    return True


INIT = {
    "fault_event_id": 1,
    "alert": {
        "alert_name": "PodOOMKilled", "severity": "critical",
        "namespace": "demo", "workload": "payment-service", "labels": {"pod": "p1"},
    },
    "evidence": [], "hypotheses": [], "rca": None, "plan": None,
    "risk_decision": None, "iteration": 0, "rediagnose_count": 0,
}


def _cfg(thread: str) -> dict:
    return {"configurable": {"thread_id": thread}}


async def main() -> None:
    fake = FakeLLM()
    G.llm = fake
    G.call_tool = fake_call_tool
    G.whitelist_validate = whitelist_ok
    # 阶段三节点打桩
    G.SessionLocal = _fake_session
    G.executor_execute = _fake_execute_ok
    G.executor_rollback = _fake_rollback_ok
    G.run_window = _fake_window_pass

    # 1) REQUIRE_APPROVAL：interrupt 挂起，risk_decision 已在状态里（gate_check 先行返回）
    r1 = await G.graph.ainvoke(dict(INIT), config=_cfg("smoke-approve"))
    assert "__interrupt__" in r1, f"应挂起，实际 keys={sorted(r1.keys())}"
    assert r1.get("rca") is not None and r1.get("plan") is not None
    assert r1["risk_decision"].policy == Policy.REQUIRE_APPROVAL, r1.get("risk_decision")
    assert r1["iteration"] == 3, f"iteration={r1['iteration']}（应为 3 轮）"
    assert len(r1["evidence"]) == 3, f"evidence={len(r1['evidence'])}（4 次调用去重后应为 3）"
    print("[1] interrupt 挂起 OK：iteration=3，证据 4 调用去重后 3 条，决策已入状态")

    # 2) approve 恢复 → 走完
    r2 = await G.graph.ainvoke(Command(resume={"approved": True}), config=_cfg("smoke-approve"))
    assert "__interrupt__" not in r2
    print("[2] approve 恢复 OK：图走完")

    # 3) reject 恢复 → 走完
    r3 = await G.graph.ainvoke(dict(INIT), config=_cfg("smoke-reject"))
    assert "__interrupt__" in r3
    r3 = await G.graph.ainvoke(Command(resume={"approved": False}), config=_cfg("smoke-reject"))
    assert "__interrupt__" not in r3
    print("[3] reject 恢复 OK：图走完")

    # 4) 白名单拒绝 → 不经 interrupt 直接结束
    G.whitelist_validate = whitelist_deny
    r4 = await G.graph.ainvoke(dict(INIT), config=_cfg("smoke-deny"))
    assert "__interrupt__" not in r4
    assert r4["risk_decision"].policy == Policy.REQUIRE_APPROVAL
    print("[4] 白名单拒绝 OK：不经 interrupt 直接结束（runner 置 failed）")

    # 5) AUTO 分支（桩 decide）→ 不挂起直接结束
    G.whitelist_validate = whitelist_ok
    G.decide = lambda action: Decision(
        action=action, risk_level=RiskLevel.LOW, policy=Policy.AUTO, reason="冒烟桩"
    )
    r5 = await G.graph.ainvoke(dict(INIT), config=_cfg("smoke-auto"))
    assert "__interrupt__" not in r5
    assert r5["risk_decision"].policy == Policy.AUTO
    print("[5] AUTO 分支 OK：不挂起直接结束")

    print("ALL GRAPH SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
