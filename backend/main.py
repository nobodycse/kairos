"""KAIROS backend 入口（uvicorn main:app）。

Phase 0 stub：全部路由返回假数据（design.md §8.1）。
PostgreSQL / Redis / K8s 在 Phase 1 接入，本阶段不连接。
"""
from api.main import app  # noqa: F401  (uvicorn 加载点)
