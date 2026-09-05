"""故障实验室：创建实验 / 执行注入 / 闭环报告（design.md §3.9–§3.11）。

Phase 0 假数据（内存态，重启即失）；Phase 3 接 fault-lab 真实注入。
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import mock
from api.deps import require_user

router = APIRouter(prefix="/experiments", tags=["experiments"])

FaultType = Literal[
    "oom",
    "cpu_overload",
    "pod_crash",
    "image_pull_backoff",
    "replica_anomaly",
    "network_latency",
    "node_not_ready",
]


class ExperimentCreate(BaseModel):
    fault_type: FaultType
    target_workload: str
    params: dict = {}


@router.post("", status_code=201, summary="创建故障实验")
def create_experiment(req: ExperimentCreate, _: str = Depends(require_user)):
    # oom 走调低 limit 方式时传 params.memory_limit（design.md §3.9）
    return mock.create_experiment(req.fault_type, req.target_workload, req.params)


@router.post("/{experiment_id}/inject", status_code=202, summary="执行注入")
def inject(experiment_id: int, _: str = Depends(require_user)):
    exp = mock.get_experiment(experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="实验不存在")
    mock.mark_injected(experiment_id)
    return {"id": experiment_id, "status": "injecting"}


@router.get("/{experiment_id}/report", summary="实验闭环报告（未结束时 result 为 null）")
def report(experiment_id: int, _: str = Depends(require_user)):
    exp = mock.get_experiment(experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="实验不存在")
    return exp
