"""诊断图（architecture.md §7.1，阶段二子集：collect → analyze → propose → risk_gate）。

阶段二不含 execute_fix/verify/rollback 节点（阶段三接在 handoff 之后）：
- FORBIDDEN / 白名单拒绝 → goto END，runner 按决策置 failed（§5.1）
- REQUIRE_APPROVAL → interrupt() 挂起，approve/reject 以 Command(resume={"approved": bool})
  恢复（design §5.2）；恢复后 risk_gate 从头重执行（白名单只读幂等，允许路径无副作用）
- AUTO / approve → goto END，runner 置 remediating 交接阶段三 executor

图节点不写业务表（落库在 runner），可脱离 DB 测试；风险白名单需要 db session，
在节点内开 SessionLocal。llm / call_tool / whitelist_validate 以模块级名字引用，
便于冒烟脚本打桩（scripts/smoke_stage2.py）。
"""
import json
import time
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from agent import events
from agent.llm import llm
from agent.prompts import ANALYZE_SYSTEM, COLLECT_SYSTEM, PROPOSE_SYSTEM
from agent.schemas import Evidence, RemediationPlan, RCAReport
from agent.state import AgentState
from agent.tools import REMEDIATION_PARAM_MODELS, ToolError, call_tool, to_openai_specs
from core.db import SessionLocal
from risk_control import Policy, decide
from risk_control.whitelist import validate as whitelist_validate

MAX_ITERATIONS = 8  # §7.1：工具调用轮次上限


def thread_id_for(fault_event_id: int) -> str:
    """MemorySaver 的 checkpoint 线程标识（一事件一线程）。"""
    return f"fault-{fault_event_id}"


def _alert_brief(alert: dict) -> str:
    """AgentState.alert（fault_events 行字段口径）→ 给 LLM 的告警摘要。"""
    labels = alert.get("labels") or {}
    pod = labels.get("pod") or "-"
    return (
        f"告警：{alert.get('alert_name')}（severity={alert.get('severity')}）\n"
        f"namespace={alert.get('namespace')}，workload={alert.get('workload') or '未知'}，pod={pod}\n"
        f"labels：{json.dumps(labels, ensure_ascii=False)}"
    )


# ---------- collect_evidence ----------


async def collect_evidence(state: AgentState) -> dict:
    """LLM function calling 循环（iteration ≤ 8，循环在节点内）。

    工具级失败（ToolError）回填错误消息让 LLM 继续调查，不炸事件；
    LLM 不再调用工具即视为「证据足够」（mermaid 的 ENOUGH 分支）。
    证据去重：同工具+同参数只保留最新一条（architecture §6.4）。
    """
    fault_event_id = state["fault_event_id"]
    messages: list[dict] = [
        {"role": "system", "content": COLLECT_SYSTEM},
        {"role": "user", "content": _alert_brief(state["alert"]) + "\n\n请开始收集证据，排查该故障。"},
    ]
    specs = to_openai_specs()
    collected: dict[str, Evidence] = {}
    iteration = 0
    while iteration < MAX_ITERATIONS:
        iteration += 1
        resp = await llm.chat(messages, tools=specs)
        if not resp.tool_calls:
            break
        messages.append(
            {
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": json.dumps(c.arguments, ensure_ascii=False),
                        },
                    }
                    for c in resp.tool_calls
                ],
            }
        )
        for call in resp.tool_calls:
            await events.emit_agent_step(
                fault_event_id,
                {
                    "iteration": iteration,
                    "phase": "collect_evidence",
                    "step": "tool_start",
                    "tool": call.name,
                    "args": call.arguments,
                },
            )
            started = time.monotonic()
            item: Evidence | None = None
            error: str | None = None
            try:
                item = await call_tool(call.name, call.arguments)
                tool_content = json.dumps(
                    {"ok": True, "summary": item.summary, "data": item.data},
                    ensure_ascii=False,
                )
            except ToolError as e:
                error = str(e)
                tool_content = json.dumps({"ok": False, "error": error}, ensure_ascii=False)
            await events.emit_agent_step(
                fault_event_id,
                {
                    "iteration": iteration,
                    "phase": "collect_evidence",
                    "step": "tool_end",
                    "tool": call.name,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    **({"evidence_summary": item.summary} if item else {"error": error}),
                },
            )
            messages.append({"role": "tool", "tool_call_id": call.id, "content": tool_content})
            if item is not None:
                key = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)}"
                collected[key] = item
    return {"evidence": list(collected.values()), "iteration": iteration}


# ---------- analyze ----------


async def analyze(state: AgentState) -> dict:
    """结构化生成 RCAReport（§6.4：evidence ≥2 条且跨 ≥2 源，不满足带反馈重试 1 次）。"""
    fault_event_id = state["fault_event_id"]
    grouped: dict[str, list[str]] = {}
    for ev_item in state.get("evidence") or []:
        grouped.setdefault(ev_item.source, []).append(
            f"[{ev_item.tool}] {ev_item.summary}｜data={json.dumps(ev_item.data, ensure_ascii=False)}"
        )
    evidence_text = "\n".join(
        f"（{source}）" + "\n".join(lines) for source, lines in grouped.items()
    ) or "（无证据）"
    messages: list[dict] = [
        {"role": "system", "content": ANALYZE_SYSTEM},
        {
            "role": "user",
            "content": f"{_alert_brief(state['alert'])}\n\n可用证据：\n{evidence_text}\n\n请输出 RCAReport JSON。",
        },
    ]
    rca = await llm.structured(messages, RCAReport)
    if not _rca_evidence_ok(rca):
        messages.append(
            {
                "role": "user",
                "content": "证据不足：RCAReport.evidence 至少 2 条且跨 2 个数据源。"
                           "请从「可用证据」中补齐后重新输出完整 JSON。",
            }
        )
        rca = await llm.structured(messages, RCAReport)
        if not _rca_evidence_ok(rca):
            sources = {e.source for e in rca.evidence}
            raise RuntimeError(
                f"RCA 证据不满足 §6.4 约束：{len(rca.evidence)} 条 / {len(sources)} 源"
            )
    await events.emit_agent_step(
        fault_event_id,
        {
            "iteration": state.get("iteration", 0),
            "phase": "analyze",
            "step": "rca_ready",
            "summary": rca.root_cause,
            "confidence": rca.confidence,
        },
    )
    return {"rca": rca}


def _rca_evidence_ok(rca: RCAReport) -> bool:
    return len(rca.evidence) >= 2 and len({e.source for e in rca.evidence}) >= 2


# ---------- propose_fix ----------


async def propose_fix(state: AgentState) -> dict:
    """结构化生成 RemediationPlan（§6.5），params 按修复工具参数模型校验。"""
    fault_event_id = state["fault_event_id"]
    rca = state["rca"]
    messages: list[dict] = [
        {"role": "system", "content": PROPOSE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"告警：{_alert_brief(state['alert'])}\n\n"
                f"根因分析：fault_type={rca.fault_type}；root_cause={rca.root_cause}；"
                f"blast_radius={rca.blast_radius}；suggestion={rca.suggestion}\n\n"
                "请输出 RemediationPlan JSON。"
            ),
        },
    ]
    plan = await llm.structured(messages, RemediationPlan)
    param_model = REMEDIATION_PARAM_MODELS.get(plan.action)
    if param_model is not None:
        try:
            param_model.model_validate(plan.params)
        except ValidationError as e:
            messages.append(
                {
                    "role": "user",
                    "content": f"params 不符合 {plan.action} 的参数模型：{e.errors()[:2]}。"
                               "请修正后重新输出完整 RemediationPlan JSON。",
                }
            )
            plan = await llm.structured(messages, RemediationPlan)
            param_model.model_validate(plan.params)  # 再失败抛 ValidationError → 事件 failed
    await events.emit_agent_step(
        fault_event_id,
        {
            "iteration": state.get("iteration", 0),
            "phase": "propose_fix",
            "step": "plan_ready",
            "action": plan.action,
            "target": plan.target,
            "params": plan.params,
        },
    )
    return {"plan": plan}


# ---------- risk_gate ----------


async def risk_gate(state: AgentState) -> Command:
    """参数白名单（§11.1，拒绝时自带审计）+ decide()（§6.6）三分支。

    REQUIRE_APPROVAL 时 interrupt() 挂起；resume 后本节点从头重执行
    （白名单只读幂等、允许路径不写审计），interrupt 返回恢复值。
    """
    plan = state["plan"]
    decision = decide(plan.action)
    async with SessionLocal() as db:
        result = await whitelist_validate(db, plan)
    if not result.allowed or decision.policy == Policy.FORBIDDEN:
        return Command(update={"risk_decision": decision}, goto=END)  # runner 置 failed
    if decision.policy == Policy.REQUIRE_APPROVAL:
        resume: Any = interrupt(
            {
                "action": plan.action,
                "target": plan.target,
                "params": plan.params,
                "risk_level": decision.risk_level.value,
                "reason": plan.reason,
            }
        )
        approved = bool(resume.get("approved")) if isinstance(resume, dict) else bool(resume)
        return Command(update={"risk_decision": decision}, goto=END)  # approved 与否由 runner 按恢复值落库
    return Command(update={"risk_decision": decision}, goto=END)  # AUTO：runner 交接 executor


# ---------- 组图 ----------


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("collect_evidence", collect_evidence)
    g.add_node("analyze", analyze)
    g.add_node("propose_fix", propose_fix)
    g.add_node("risk_gate", risk_gate)
    g.add_edge(START, "collect_evidence")
    g.add_edge("collect_evidence", "analyze")
    g.add_edge("analyze", "propose_fix")
    g.add_edge("propose_fix", "risk_gate")
    # risk_gate 的分支用 Command(goto) 表达，不声明静态出边
    return g.compile(checkpointer=MemorySaver())


graph = build_graph()
