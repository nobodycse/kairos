"""FastAPI 实例与路由注册（architecture.md §6.1）。

Phase 0 stub：18 个端点（17 REST + 1 SSE）全部返回假数据，
前端对着部署后的 /docs 开发（design.md §8.3 协作约定）。
Phase 1 起：登录真连 users 表，cluster/faults 等仍为 stub。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import auth, cluster, experiments, faults, reports, settings, webhooks
from agent.runner import recover_orphans
from core.db import engine
from core.redis import close as redis_close, ping as redis_ping
from faultlab import recover_experiments
from monitoring.clients import close_clients, init_clients


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_clients()
    await redis_ping()
    await recover_orphans()  # §5.1：重启孤儿事件统一置 failed，不自动续跑
    await recover_experiments()  # phase3：injected 实验恢复评估监听，injecting 遗留回 created
    yield
    await close_clients()
    await redis_close()
    await engine.dispose()


app = FastAPI(
    title="KAIROS API",
    version="0.2.0",
    description=(
        "Phase 1：登录接真实 users 表；其余路由仍返回假数据，契约见 docs/design.md §3/§4。\n\n"
        "示例统一使用同一个故事：demo namespace 的 payment-service 发生 OOM。\n"
        "SSE 端点 /api/v1/faults/{id}/stream 的契约见 design.md §4（/docs 覆盖不了 SSE）。"
    ),
    lifespan=lifespan,
)

# Phase 0 开发期放开跨域（前端本地 Vite devServer 直连）；Phase 1 收敛到部署域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["health"], summary="存活探针（无鉴权，CI/CD 健康检查用）")
def health():
    return {"status": "ok", "version": "0.1.0"}


for _router in (auth, cluster, faults, experiments, reports, settings, webhooks):
    app.include_router(_router.router, prefix="/api/v1")
