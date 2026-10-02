"""评估报告汇总（design.md §3.12）。

Phase 3 起从 experiment_results × experiments 实时聚合（替换 Phase 0 mock）。
比率分母：diagnosed_correctly / auto_recovered 取非 null 行（detected=false
不计入准确率/恢复率）；false_action 为 NOT NULL 布尔，按全部已评估行取均值。
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select

from api.deps import require_user
from core.db import get_db
from models import Experiment, ExperimentResult

router = APIRouter(prefix="/reports", tags=["reports"])


def _ratio(trues: int, total: int) -> float | None:
    return round(trues / total, 4) if total else None


def _avg(values: list) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _stats(rows: list[ExperimentResult]) -> dict:
    acc = [r for r in rows if r.diagnosed_correctly is not None]
    rec = [r for r in rows if r.auto_recovered is not None]
    mttrs = [r.mttr_s for r in rows if r.mttr_s is not None]
    return {
        "diagnosis_accuracy": _ratio(
            sum(1 for r in acc if r.diagnosed_correctly), len(acc)
        ),
        "recovery_rate": _ratio(sum(1 for r in rec if r.auto_recovered), len(rec)),
        "avg_mttr_s": _avg(mttrs),
        "false_action_rate": _ratio(sum(1 for r in rows if r.false_action), len(rows)),
    }


@router.get("/summary", summary="评估汇总（诊断准确率 / 自动恢复率 / MTTR / 误操作率，§3.12）")
async def summary(db=Depends(get_db), _: str = Depends(require_user)):
    stmt = select(Experiment)
    experiments = list((await db.execute(stmt)).scalars().all())
    stmt_results = select(ExperimentResult)
    results = list((await db.execute(stmt_results)).scalars().all())

    by_exp: dict[int, list[ExperimentResult]] = {}
    for r in results:
        by_exp.setdefault(r.experiment_id, []).append(r)
    # 每实验取最新一条结果（评估器幂等，防御重复行）
    latest = [max(rows, key=lambda r: (r.created_at, r.id)) for rows in by_exp.values()]
    fault_type_of = {e.id: e.fault_type for e in experiments}

    groups: dict[str, list[ExperimentResult]] = {}
    for r in latest:
        ft = fault_type_of.get(r.experiment_id)
        if ft is not None:
            groups.setdefault(ft, []).append(r)

    return {
        "total_experiments": len(experiments),
        "overall": _stats(latest),
        "by_fault_type": [
            {"fault_type": ft, "runs": len(rows), **_stats(rows)}
            for ft, rows in sorted(groups.items())
        ],
    }
