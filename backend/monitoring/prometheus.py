"""Prometheus HTTP API 客户端（architecture.md §6.2）。

只封装 /api/v1/query 与 /api/v1/query_range，返回 monitoring.models 的
Sample/Series。网络错误向上抛（cluster 路由按软依赖语义捕获后置 null）。
"""
import time

import httpx

from monitoring.models import Sample, Series


class PrometheusClient:
    def __init__(self, base_url: str, timeout: float = 5.0):
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def query(self, expr: str, at: float | None = None) -> list[Sample]:
        """即时查询。at 为 unix 秒，None 表示 now。"""
        params: dict[str, str] = {"query": expr}
        if at is not None:
            params["time"] = f"{at:.3f}"
        data = await self._get("/api/v1/query", params)
        out: list[Sample] = []
        for r in data.get("result", []):
            try:
                ts, val = r["value"]
                out.append(
                    Sample(metric=r.get("metric", {}), value=float(val), timestamp=float(ts))
                )
            except (KeyError, ValueError):
                continue
        return out

    async def query_range(
        self, expr: str, start: float, end: float, step: int
    ) -> list[Series]:
        """范围查询，start/end 为 unix 秒，step 为秒。"""
        data = await self._get(
            "/api/v1/query_range",
            {"query": expr, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": step},
        )
        out: list[Series] = []
        for r in data.get("result", []):
            values = []
            for ts, val in r.get("values", []):
                try:
                    values.append((float(ts), float(val)))
                except ValueError:
                    continue
            out.append(Series(metric=r.get("metric", {}), values=values))
        return out

    async def _get(self, path: str, params: dict[str, str]) -> dict:
        resp = await self._client.get(f"{self._base_url}{path}", params=params)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"prometheus query failed: {payload}")
        return payload.get("data", {})


def now_ts() -> float:
    return time.time()
