"""audit_logs 写入原语（design.md §2 DDL L159-169 / architecture.md §11.1）。

审计范围（文档约定）：白名单拒绝（denied）、Decision != FORBIDDEN 的动作执行
前后（executor 阶段三写 success/failure，含快照 diff）、人工 approve/reject
（actor 为 user:xxx）、agent 未捕获异常（failure，design §5.1）。
阶段一先提供写入原语，消费方随阶段二/三接线。
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from models import AuditLog

logger = logging.getLogger(__name__)

RESULT_VALUES = ("allowed", "denied", "success", "failure")


def resource_for(kind: str, namespace: str, name: str) -> str:
    """资源标识，如 resource_for("deployment", "demo", "payment-service")
    → "deployment/demo/payment-service"（design.md §2 resource 列注释）。"""
    return f"{kind}/{namespace}/{name}"


async def write_audit(
    db: AsyncSession,
    *,
    actor: str,
    action: str,
    resource: str,
    result: str,
    params: dict | None = None,
    detail: dict | None = None,
) -> None:
    """写一条审计并独立提交——审计是追加式留痕，不随调用方事务回滚丢失。

    写入失败只记日志不上抛：审计不应阻断主流程（主流程的失败有各自的
    failed 状态迁移路径）。
    """
    if result not in RESULT_VALUES:
        raise ValueError(f"非法审计 result：{result}（应为 {RESULT_VALUES} 之一）")
    try:
        db.add(
            AuditLog(
                actor=actor,
                action=action,
                resource=resource,
                params=params or {},
                result=result,
                detail=detail,
            )
        )
        await db.commit()
    except Exception:
        logger.exception("audit_logs 写入失败（action=%s resource=%s）", action, resource)
        await db.rollback()
