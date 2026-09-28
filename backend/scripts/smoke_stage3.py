"""阶段三本地冒烟测试：假 executor/假验证窗口/假 DB 走查执行-验证-回滚链路。

用法：python backend/scripts/smoke_stage3.py

覆盖（均为 REQUIRE_APPROVAL → Command(resume={"approved": True}) 恢复后）：
1. 执行成功 + 验证全过 → outcome=resolved（事件停在 verifying）
2. 验证不过 + 回滚成功 + count<2 → outcome=redo（count 递增、事件回 diagnosing）
3. 第二次验证不过 → redo（count 1→2）
4. count 用尽（=2）+ 回滚成功 → outcome=failed（事件停在 rolling_back）
5. 回滚失败 → outcome=failed
6. 执行失败 → outcome=exec_failed（不进验证窗口）
"""
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.disable(logging.CRITICAL)  # 冒烟静音：events 的 best-effort Redis 日志不关心

import agent.graph as G  # noqa: E402
from agent.llm import LLMResponse, ToolCall  # noqa: E402
from agent.schemas import Evidence, RemediationPlan, RCAReport  # noqa: E402
from langgraph.types import Command  # noqa: E402
from remediation.executor import ExecResult  # noqa: E402
from risk_control.whitelist import WhitelistResult  # noqa: E402

CANNED_EVIDENCE = [
    Evidence(source="k8s_api", tool="get_pod_status", summary="重启 3 次", data={"restarts": 3}),
    Evidence(source="events", tool="get_k8s_events", summary="OOMKilled x2", data={"reasons": ["OOMKilled"]}),
    Evidence(source="loki", tool="get_pod_logs", summary="out of memory", data={"lines": 5}),
]


class FakeLLM:
    def __init__(self):
        self.chat_round = 0

    async def chat(self, messages, tools=None):
        self.chat_round += 1
        if self.chat_round == 1:
            return LLMResponse(
                content="查状态与事件",
                tool_calls=[
                    ToolCall(id="c1", name="get_pod_status", arguments={"namespace": "demo", "name": "p1"}),
                    ToolCall(id="c2", name="get_k8s_events", arguments={"namespace": "demo", "pod": "p1"}),
                ],
            )
        return LLMResponse(content="证据足够：OOM。", tool_calls=[])

    async def structured(self, messages, schema):
        if schema is RCAReport:
            return RCAReport(
                fault_type="OOM", root_cause="内存超限", evidence=CANNED_EVIDENCE[:2],
                confidence=0.9, blast_radius="demo/payment-service", suggestion="重启",
            )
        if schema is RemediationPlan:
            return RemediationPlan(
                action="restart_deployment", namespace="demo",
                target="payment-service", params={}, reason="清除异常状态",
            )
        raise AssertionError(f"未预期的 schema：{schema}")


async def fake_call_tool(name, arguments):
    by_tool = {e.tool: e for e in CANNED_EVIDENCE}
    return by_tool[name]


class _FakeEv:
    def __init__(self):
        self.reset()

    def reset(self, count: int = 0) -> None:
        self.status = "awaiting_approval"
        self.rediagnose_count = count
        self.updated_at = None


EV = _FakeEv()


class _FakeDB:
    async def get(self, model, key):
        return EV

    async def commit(self):
        return None


@asynccontextmanager
async def fake_session():
    yield _FakeDB()


EXEC = {"ok": True}
ROLLBACK = {"ok": True}
WINDOW = {"passed": True}


async def fake_execute(db, fault_event_id, plan, decision):
    return ExecResult(success=EXEC["ok"], message="stub")


async def fake_rollback(db, fault_event_id, plan):
    return ExecResult(success=ROLLBACK["ok"], message="stub")


async def fake_window(fault_event_id, namespace, target):
    return WINDOW["passed"]


async def whitelist_ok(db, plan):
    return WhitelistResult(allowed=True, violations=[])


INIT = {
    "fault_event_id": 1,
    "alert": {
        "alert_name": "PodOOMKilled", "severity": "critical",
        "namespace": "demo", "workload": "payment-service", "labels": {"pod": "p1"},
    },
    "evidence": [], "hypotheses": [], "rca": None, "plan": None,
    "risk_decision": None, "iteration": 0, "rediagnose_count": 0, "outcome": None,
}


def _cfg(thread: str) -> dict:
    return {"configurable": {"thread_id": thread}}


async def run_approved(thread: str) -> dict:
    r = await G.graph.ainvoke(dict(INIT), config=_cfg(thread))
    assert "__interrupt__" in r, "应先在 gate_wait 挂起"
    return await G.graph.ainvoke(Command(resume={"approved": True}), config=_cfg(thread))


async def main() -> None:
    G.whitelist_validate = whitelist_ok
    G.SessionLocal = fake_session
    G.executor_execute = fake_execute
    G.executor_rollback = fake_rollback
    G.run_window = fake_window

    # 1) 执行成功 + 验证全过 → resolved
    EXEC["ok"] = WINDOW["passed"] = ROLLBACK["ok"] = True
    EV.reset()
    G.llm = FakeLLM()
    r = await run_approved("s3-1")
    assert r.get("outcome") == "resolved" and EV.status == "verifying", (r.get("outcome"), EV.status)
    print("[1] 执行成功 + 验证全过 → resolved OK")

    # 2) 验证不过 + 回滚成功 → redo（count 0→1，回 diagnosing）
    WINDOW["passed"] = False
    EV.reset()
    G.llm = FakeLLM()
    r = await run_approved("s3-2")
    assert r.get("outcome") == "redo" and EV.rediagnose_count == 1 and EV.status == "diagnosing", (
        r.get("outcome"), EV.rediagnose_count, EV.status
    )
    print("[2] 验证不过 + 回滚成功 → redo OK（count=1，事件回 diagnosing）")

    # 3) 第二次验证不过 → redo（count 1→2）
    EV.reset(count=1)
    G.llm = FakeLLM()
    r = await run_approved("s3-3")
    assert r.get("outcome") == "redo" and EV.rediagnose_count == 2, (r.get("outcome"), EV.rediagnose_count)
    print("[3] 第二次验证不过 → redo OK（count=2）")

    # 4) count 用尽 + 回滚成功 → failed
    EV.reset(count=2)
    G.llm = FakeLLM()
    r = await run_approved("s3-4")
    assert r.get("outcome") == "failed" and EV.rediagnose_count == 2 and EV.status == "rolling_back", (
        r.get("outcome"), EV.rediagnose_count, EV.status
    )
    print("[4] 重诊断次数用尽 → failed OK（事件停在 rolling_back 待 runner 置 failed）")

    # 5) 回滚失败 → failed
    EV.reset()
    ROLLBACK["ok"] = False
    G.llm = FakeLLM()
    r = await run_approved("s3-5")
    assert r.get("outcome") == "failed", r.get("outcome")
    ROLLBACK["ok"] = True
    print("[5] 回滚失败 → failed OK")

    # 6) 执行失败 → exec_failed（不进验证窗口）
    EXEC["ok"] = False
    EV.reset()
    G.llm = FakeLLM()
    r = await run_approved("s3-6")
    assert r.get("outcome") == "exec_failed", r.get("outcome")
    EXEC["ok"] = True
    print("[6] 执行失败 → exec_failed OK（未进验证窗口）")

    print("ALL STAGE-3 GRAPH SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
