"""AgentState —— architecture.md §7.1 的原文结构。

evidence 是追加式证据链：Annotated 的 add reducer（operator.add）让节点只
返回增量列表，langgraph 负责合并；iteration 上限 8，rediagnose_count 上限 2
（两处上限的含义见 design.md §5.1）。
"""
import operator
from typing import Annotated, TypedDict

from agent.schemas import Evidence, RemediationPlan, RCAReport
from risk_control import Decision


class AgentState(TypedDict):
    fault_event_id: int
    alert: dict  # 原始告警（fault_events.labels + 上下文）
    evidence: Annotated[list[Evidence], operator.add]  # 追加式证据链
    hypotheses: list[str]  # 已排除/待验证假设
    rca: RCAReport | None
    plan: RemediationPlan | None
    risk_decision: Decision | None
    iteration: int  # 工具调用轮次上限 8
    rediagnose_count: int  # 回滚后重诊断次数上限 2
