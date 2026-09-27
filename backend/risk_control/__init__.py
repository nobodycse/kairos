"""风险控制（architecture.md §6.6）：策略表 + decide()。

工具隔离（§7.2/§11.1）：本模块不向 LLM 暴露任何可调用接口。修复动作只能来自
propose_fix 的结构化提议，先过 whitelist.py 参数白名单，再由 decide() 给出
AUTO / REQUIRE_APPROVAL / FORBIDDEN 决策（阶段二 risk_gate 节点的分支依据）。
"""
from enum import StrEnum

from pydantic import BaseModel


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Policy(StrEnum):
    AUTO = "auto"
    REQUIRE_APPROVAL = "require_approval"
    FORBIDDEN = "forbidden"


# action → (风险等级, 默认策略)；未注册动作一律 CRITICAL/FORBIDDEN（默认拒绝）
RISK_POLICY: dict[str, tuple[RiskLevel, Policy]] = {
    "update_resource_limit": (RiskLevel.MEDIUM, Policy.REQUIRE_APPROVAL),
    "scale_deployment": (RiskLevel.MEDIUM, Policy.REQUIRE_APPROVAL),
    "restart_deployment": (RiskLevel.MEDIUM, Policy.REQUIRE_APPROVAL),
    "rollback_deployment": (RiskLevel.HIGH, Policy.REQUIRE_APPROVAL),
    "delete_node": (RiskLevel.CRITICAL, Policy.FORBIDDEN),
}


class Decision(BaseModel):
    action: str
    risk_level: RiskLevel
    policy: Policy
    reason: str


def decide(action: str) -> Decision:
    """动作 → 风险决策。未注册动作默认拒绝（§6.6 注 / §11.1）。"""
    hit = RISK_POLICY.get(action)
    if hit is None:
        return Decision(
            action=action,
            risk_level=RiskLevel.CRITICAL,
            policy=Policy.FORBIDDEN,
            reason=f"未注册动作 {action}，默认拒绝",
        )
    level, policy = hit
    return Decision(
        action=action,
        risk_level=level,
        policy=policy,
        reason=f"RISK_POLICY：{action} → ({level.value}, {policy.value})",
    )
