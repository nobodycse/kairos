"""Agent 公共数据模型 —— architecture.md §6.4/§6.5 的原文 schema。

Evidence 是证据链的基本单元（工具层产出、diagnoses.evidence 落库、SSE
evidence_summary 展示共用）；RCAReport/RemediationPlan 是 LLM 结构化输出的
目标 schema。risk_level/policy 不进 RemediationPlan——由 risk_control 派生。
"""
from typing import Any, Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    """一条证据（architecture.md §6.4 L334）。data 为截断后的原始数据。"""

    source: Literal["k8s_api", "prometheus", "loki", "events"]
    tool: str  # 产生证据的工具名
    summary: str  # 一句话摘要（SSE tool_end 的 evidence_summary 同源）
    data: dict[str, Any]


class RCAReport(BaseModel):
    """根因分析报告（architecture.md §6.4 L340）。

    evidence ≥2 条且至少跨 2 个数据源的约束由 analyze 节点校验（阶段二），
    schema 层不硬卡——LLM 少给证据时优先走带错误重试而非直接拒绝。
    """

    fault_type: str  # OOM / CrashLoop / CPUThrottling / ...
    root_cause: str
    evidence: list[Evidence]
    confidence: float = Field(ge=0, le=1)
    blast_radius: str
    suggestion: str


class RemediationPlan(BaseModel):
    """修复方案提议（architecture.md §6.5 L354）。

    只能由 propose_fix 节点结构化输出产生（§7.2 工具隔离），经
    risk_control.whitelist 校验 + decide() 决策后才可能被执行。
    """

    action: Literal[
        "update_resource_limit", "scale_deployment",
        "restart_deployment", "rollback_deployment", "delete_pod",
    ]
    namespace: str
    target: str  # workload 名（Deployment）；delete_pod 时为 Pod 名
    params: dict[str, Any]  # 如 {"container": "app", "memory_limit": "1Gi"}
    reason: str
