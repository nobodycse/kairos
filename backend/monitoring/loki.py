"""Loki 查询客户端（architecture.md §6.2）。

Phase 1 无 REST 消费方（首个消费方是 Phase 2 的 Agent 工具 get_pod_logs 与
verification 的 logs_clean 检查），实现后先以脚本/容器内手动方式验证。
"""
from datetime import datetime, timezone

import httpx


class LogLine:
    """单条日志：纳秒时间戳 + 行文本 + 标签集。"""

    __slots__ = ("ts_ns", "line", "labels")

    def __init__(self, ts_ns: int, line: str, labels: dict[str, str]):
        self.ts_ns = ts_ns
        self.line = line
        self.labels = labels

    @property
    def timestamp(self) -> datetime:
        return datetime.fromtimestamp(self.ts_ns / 1e9, tz=timezone.utc)

    def __repr__(self) -> str:  # pragma: no cover
        return f"LogLine({self.timestamp.isoformat()}, {self.line[:60]!r})"


class LokiClient:
    def __init__(self, base_url: str, timeout: float = 5.0):
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def query_range(
        self, logql: str, start: datetime, end: datetime, limit: int = 100
    ) -> list[LogLine]:
        """范围查询。logql 例：{namespace="demo", pod=~"payment-.*"} |= "ERROR"。"""
        params = {
            "query": logql,
            "start": str(int(start.timestamp() * 1e9)),
            "end": str(int(end.timestamp() * 1e9)),
            "limit": str(limit),
            "direction": "backward",
        }
        resp = await self._client.get(f"{self._base_url}/loki/api/v1/query_range", params=params)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"loki query failed: {payload}")
        out: list[LogLine] = []
        for stream in payload.get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            for ts_ns, line in stream.get("values", []):
                out.append(LogLine(int(ts_ns), line, labels))
        return out
