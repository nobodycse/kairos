"""Redis 异步客户端单例（design.md §5.3：告警指纹去重、事件锁）。

连接在 app lifespan 里 ping 验证（api/main.py）。
"""
import redis.asyncio as redis

from core.config import settings

r = redis.from_url(settings.redis_url, decode_responses=True)


async def ping() -> None:
    await r.ping()


async def close() -> None:
    await r.aclose()
