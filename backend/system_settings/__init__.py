"""系统内配置（Phase 2.5 增补）：AI 供应商配置存 DB，管理员在系统设置页维护。

优先级：DB 配置 > .env（未配置时兜底，现网行为不破坏；UI 保存即覆盖）。
安全口径：DB 明文存储，API/审计只回掩码（sk-***后4），永不回明文；
配置修改写审计并即时失效进程内缓存（单 backend 进程；多 worker 场景由
10s TTL 兜底）。
"""
import time

from pydantic import BaseModel

from agent.llm import LLMClient
from core.config import settings
from models import SystemSetting
from risk_control.audit import write_audit

LLM_SETTING_KEY = "llm"
_CACHE_TTL = 10.0  # 秒；保存时主动失效，多 worker 场景以此兜底

# None 表示"DB 无配置"（区别于"未读过"）；expires 为 0 表示缓存已失效
_cache: dict = {"config": None, "expires": 0.0}


class LLMConfig(BaseModel):
    base_url: str
    api_key: str
    model: str


def mask_key(key: str) -> str:
    """key 掩码：前 3 + *** + 后 4；过短只保留前 3。"""
    if not key:
        return ""
    if len(key) <= 7:
        return f"{key[:3]}***"
    return f"{key[:3]}***{key[-4:]}"


def invalidate_cache() -> None:
    _cache["expires"] = 0.0


async def get_llm_config(db) -> LLMConfig | None:
    """读 DB 里的 LLM 配置（带 TTL 缓存）；无配置返回 None（调用方回落 .env）。"""
    now = time.monotonic()
    if _cache["expires"] > now:
        return _cache["config"]
    row = await db.get(SystemSetting, LLM_SETTING_KEY)
    config = LLMConfig(**row.value) if row is not None and row.value else None
    _cache["config"] = config
    _cache["expires"] = now + _CACHE_TTL
    return config


async def save_llm_config(db, *, base_url: str, api_key: str, model: str, actor: str) -> LLMConfig:
    """upsert 配置 + 审计（detail 不含完整 key）+ 失效缓存。

    api_key 传空 = 保持已保存的 key（前端"留空不修改"）；无已保存 key 时为
    必填，否则抛 ValueError（路由层转 422）。
    """
    row = await db.get(SystemSetting, LLM_SETTING_KEY)
    existing = LLMConfig(**row.value) if row is not None and row.value else None
    key_value = api_key.strip() or (existing.api_key if existing else "")
    if not key_value:
        raise ValueError("api_key 为空且当前没有已保存的 key")
    config = LLMConfig(
        base_url=base_url.strip(),
        api_key=key_value,
        model=model.strip(),
    )
    if row is None:
        row = SystemSetting(key=LLM_SETTING_KEY)
        db.add(row)
    row.value = config.model_dump()
    await db.commit()
    invalidate_cache()
    await write_audit(
        db,
        actor=actor,
        action="update_llm_settings",
        resource="system_settings/llm",
        params={"base_url": config.base_url, "model": config.model},
        result="success",
        detail={"api_key_masked": mask_key(config.api_key)},
    )
    return config


async def get_llm(db) -> LLMClient | None:
    """当前生效的 LLM 客户端：DB 配置优先，回落 .env；两者皆无返回 None。"""
    config = await get_llm_config(db)
    if config is not None:
        return LLMClient(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            timeout=settings.llm_timeout_seconds,
        )
    if settings.llm_api_key:
        return LLMClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout_seconds,
        )
    return None


async def llm_ready(db) -> bool:
    """诊断前置检查：DB 或 .env 任一配置了 key 即就绪。"""
    return await get_llm(db) is not None
