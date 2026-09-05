"""评估报告汇总（design.md §3.12）。"""
from fastapi import APIRouter, Depends

from api import mock
from api.deps import require_user

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/summary", summary="评估汇总（诊断准确率 / 自动修复成功率 / MTTR / 误操作率）")
def summary(_: str = Depends(require_user)):
    return mock.REPORTS_SUMMARY
