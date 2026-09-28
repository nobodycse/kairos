"""诊断图（architecture.md §7.1，阶段二+三：collect → analyze → propose → gate
→ execute_fix → verify → rollback 循环）。

- gate_check：白名单（§11.1）+ decide()（§6.6），决策先落 state 再路由——
  FORBIDDEN/拒绝 → END（runner 置 failed）；AUTO/approve → execute_fix；
  REQUIRE_APPROVAL → gate_wait。
- gate_wait：interrupt() 挂起，approve/reject 以 Command(resume={"approved": bool})
  恢复（design §5.2）；reject 的落库语义在 runner（接口已同步改库），图直接
  goto END；approve 落到 execute_fix。
- execute_fix：executor.execute（§6.5，快照先落库再 patch）→ 成功置 verifying
  → verify；失败 outcome=exec_failed → END（runner 置 failed）。
- verify：verification.run_window（§6.7，3 分钟窗口）→ 全过 outcome=resolved →
  END（runner 置 resolved+mttr）；不过 → rollback 节点。
- rollback：executor.rollback（按快照恢复）→ 成功且 rediagnose_count<2 →
  count+1、置 diagnosing、outcome=redo → END（runner 放锁后重入新线程）；
  否则 outcome=failed → END。图内不回边 collect——evidence 是追加式 reducer，
  跨轮重跑会污染上下文，重诊断由 runner 起新线程等价实现（落地决策 §一.9）。

图节点内需要 DB 的操作各自开 SessionLocal；llm / call_tool / whitelist_validate /
executor / verification 以模块级名字引用，便于冒烟脚本打桩（scripts/smoke_stage*.py）。
"""
import json
import time
from datetime import datetime, timezone
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent import events
from agent.llm import llm
from agent.prompts import ANALYZE_SYSTEM, COLLECT_SYSTEM, PROPOSE_SYSTEM
from agent.schemas import Evidence, RemediationPlan, RCAReport
from agent.state import AgentState
from agent.tools import REMEDIATION_PARAM_MODELS, ToolError, call_tool, to_openai_specs
from core.db import SessionLocal
from models import FaultEvent
from risk_control import Decision, Policy, RiskLevel, decide
from risk_control.whitelist import validate as whitelist_validate
from remediation.executor import execute as executor_execute
from remediation.executor import rollback as executor_rollback
from verification import run_window

MAX_ITERATIONS = 8  # §7.1：工具调用轮次上限


def thread_id_for(fault_event_id: int, round_count: int = 0) -> str:
    """MemorySaver 的 checkpoint 线程标识（一事件一轮一线程，回滚重诊断进新线程）。"""
    return f"fault-{fault_event_id}-r{round_count}"


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


# ---------- risk_gate（拆两节点：决策路由 / interrupt 挂起） ----------


async def gate_check(state: AgentState) -> Command:
    """参数白名单（§11.1，拒绝时自带审计）+ decide()（§6.6）。

    决策先落 state（interrupt 前可见，runner 据此写 remediation_actions），
    再按三分支路由：FORBIDDEN/拒绝 → END（runner 置 failed）；AUTO → END
    （runner 交接 executor）；REQUIRE_APPROVAL → gate_wait。
    """
    plan = state["plan"]
    decision = decide(plan.action)
    async with SessionLocal() as db:
        result = await whitelist_validate(db, plan)
    if not result.allowed or decision.policy == Policy.FORBIDDEN:
        return Command(update={"risk_decision": decision}, goto=END)
    if decision.policy == Policy.REQUIRE_APPROVAL:
        return Command(update={"risk_decision": decision}, goto="gate_wait")
    return Command(update={"risk_decision": decision}, goto="execute_fix")  # AUTO


async def gate_wait(state: AgentState) -> Command:
    """interrupt() 挂起点：恢复后本节点重执行，interrupt 直接返回恢复值。

    reject（approved=False）→ END（closed 已由接口落库）；approve → execute_fix。
    """
    plan = state["plan"]
    decision = state["risk_decision"]
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
    if approved:
        return Command(goto="execute_fix")
    return Command(goto=END)


# ---------- execute_fix / verify / rollback（阶段三） ----------


async def _set_status(db: AsyncSession, fault_event_id: int, to: str, reason: str) -> None:
    """节点内状态迁移 + status_changed 事件（迁移点在节点，终态收尾在 runner）。"""
    ev = await db.get(FaultEvent, fault_event_id)
    if ev is None or ev.status == to:
        return
    await events.emit_status_changed(fault_event_id, ev.status, to, reason)
    ev.status = to
    ev.updated_at = datetime.now(timezone.utc)
    await db.commit()


async def execute_fix(state: AgentState) -> Command | dict:
    """执行修复（§6.5）：成功 → 置 verifying → verify；失败 → exec_failed → END。"""
    fault_event_id = state["fault_event_id"]
    plan, decision = state["plan"], state["risk_decision"]
    async with SessionLocal() as db:
        await _set_status(db, fault_event_id, "remediating", "开始执行修复")
        result = await executor_execute(db, fault_event_id, plan, decision)
    await events.emit_agent_step(
        fault_event_id,
        {
            "iteration": state.get("iteration", 0),
            "phase": "execute_fix",
            "step": "executed" if result.success else "execute_failed",
            "tool": plan.action,
            "evidence_summary": result.message,
        },
    )
    if not result.success:
        return Command(update={"outcome": "exec_failed"}, goto=END)
    async with SessionLocal() as db:
        await _set_status(db, fault_event_id, "verifying", "修复执行成功，进入观察窗口")
    return Command(goto="verify")


async def verify(state: AgentState) -> Command:
    """验证观察窗口（§6.7）：全过 → resolved；任一采样点不过 → 回滚。"""
    plan = state["plan"]
    passed = await run_window(state["fault_event_id"], plan.namespace, plan.target)
    if passed:
        return Command(update={"outcome": "resolved"}, goto=END)
    return Command(goto="rollback_node")


async def rollback_node(state: AgentState) -> Command:
    """验证不过的自动回滚（§7.1 RB 分支）：成功且重诊断 <2 次 → redo 重入。"""
    fault_event_id = state["fault_event_id"]
    plan = state["plan"]
    async with SessionLocal() as db:
        await _set_status(db, fault_event_id, "rolling_back", "验证未通过，回滚变更")
        result = await executor_rollback(db, fault_event_id, plan)
        if not result.success:
            return Command(update={"outcome": "failed"}, goto=END)
        ev = await db.get(FaultEvent, fault_event_id)
        if ev is None:
            return Command(update={"outcome": "failed"}, goto=END)
        if (ev.rediagnose_count or 0) < 2:
            ev.rediagnose_count = (ev.rediagnose_count or 0) + 1
            count = ev.rediagnose_count
            await _set_status(
                db, fault_event_id, "diagnosing", f"回滚完成，第 {count} 次重诊断"
            )
            return Command(update={"outcome": "redo"}, goto=END)
    return Command(update={"outcome": "failed"}, goto=END)  # 重诊断次数用尽，转人工


# ---------- 组图 ----------


def _build_checkpointer() -> MemorySaver:
    """checkpoint 序列化显式白名单（进程内 MemorySaver，跨进程不恢复，§5.1）。

    langgraph 未来版本将阻断未注册类型的 msgpack 反序列化，这里把图状态里
    的项目类型（pydantic 模型/枚举）注册进 allowlist，升级即不受影响。
    """
    serde = JsonPlusSerializer(allowed_msgpack_modules=None).with_msgpack_allowlist(
        [Evidence, RCAReport, RemediationPlan, Decision, RiskLevel, Policy]
    )
    return MemorySaver(serde=serde)


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("collect_evidence", collect_evidence)
    g.add_node("analyze", analyze)
    g.add_node("propose_fix", propose_fix)
    g.add_node("gate_check", gate_check)
    g.add_node("gate_wait", gate_wait)
    g.add_node("execute_fix", execute_fix)
    g.add_node("verify", verify)
    g.add_node("rollback_node", rollback_node)
    g.add_edge(START, "collect_evidence")
    g.add_edge("collect_evidence", "analyze")
    g.add_edge("analyze", "propose_fix")
    g.add_edge("propose_fix", "gate_check")
    # 可分支节点（gate_check/gate_wait/execute_fix/verify/rollback_node）一律
    # Command(goto) 路由，不声明静态出边（langgraph 不允许两者混用）
    return g.compile(checkpointer=_build_checkpointer())


graph = build_graph()
