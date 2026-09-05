"""集群只读接口：总览 / Pod / Node / Deployment / Events（design.md §3.2–§3.4）。

Phase 0 返回假数据；Phase 1 由 monitoring/ 三客户端接真实数据
（K8s API + Prometheus，architecture.md §6.2）。
"""
from fastapi import APIRouter, Depends, Query

from api import mock
from api.deps import require_user

router = APIRouter(prefix="/cluster", tags=["cluster"])


@router.get("/overview", summary="集群总览（Dashboard 卡片 + 趋势图数据源）")
def overview(_: str = Depends(require_user)):
    return mock.CLUSTER_OVERVIEW


def _paginate(items: list[dict], page: int, page_size: int) -> dict:
    start = (page - 1) * page_size
    return {
        "items": items[start : start + page_size],
        "total": len(items),
        "page": page,
        "page_size": page_size,
    }


@router.get("/pods", summary="Pod 列表（支持 namespace 过滤）")
def list_pods(
    namespace: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _: str = Depends(require_user),
):
    items = [p for p in mock.PODS if namespace is None or p["namespace"] == namespace]
    return _paginate(items, page, page_size)


@router.get("/nodes", summary="节点列表")
def list_nodes(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _: str = Depends(require_user),
):
    return _paginate(mock.NODES, page, page_size)


@router.get("/deployments", summary="Deployment 列表")
def list_deployments(
    namespace: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _: str = Depends(require_user),
):
    items = [d for d in mock.DEPLOYMENTS if namespace is None or d["namespace"] == namespace]
    return _paginate(items, page, page_size)


@router.get("/events", summary="K8s 事件（默认只返回 Warning）")
def list_events(
    type: str = "Warning",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _: str = Depends(require_user),
):
    items = [e for e in mock.EVENTS if e["type"].lower() == type.lower()]
    return _paginate(items, page, page_size)
