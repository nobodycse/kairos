"""系统设置（Phase 2.5）：AI 供应商配置的查看 / 保存 / 测试连接。

GET/PUT 均不返回明文 key（掩码 sk-***后4）；PUT api_key 留空 = 保持已保存值；
修改写审计（actor=user:xxx，detail 只含掩码）。DB 配置优先，.env 兜底。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from agent.llm import LLMClient
from api.deps import require_user
from core.config import settings
from core.db import get_db
from system_settings import get_llm_config, mask_key, save_llm_config

router = APIRouter(prefix="/settings", tags=["settings"])


class LlmConfigBody(BaseModel):
    base_url: str = ""
    model: str = ""
    api_key: str = ""  # 留空 = 保持已保存的 key


def _view(base_url: str, model: str, key: str, source: str, configured: bool) -> dict:
    return {
        "configured": configured,
        "base_url": base_url,
        "model": model,
        "api_key_masked": mask_key(key),
        "source": source,
    }


@router.get("/llm", summary="查看 AI 配置（key 掩码）")
async def get_llm_view(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_user),
):
    cfg = await get_llm_config(db)
    if cfg is not None:
        return _view(cfg.base_url, cfg.model, cfg.api_key, "db", True)
    if settings.llm_api_key:
        return _view(settings.llm_base_url, settings.llm_model, settings.llm_api_key, "env", True)
    return _view(settings.llm_base_url, settings.llm_model, "", "none", False)


@router.put("/llm", summary="保存 AI 配置（api_key 留空 = 保持现有）")
async def put_llm(
    body: LlmConfigBody,
    db: AsyncSession = Depends(get_db),
    username: str = Depends(require_user),
):
    if not body.base_url.strip() or not body.model.strip():
        raise HTTPException(status_code=422, detail="base_url 与 model 不能为空")
    try:
        cfg = await save_llm_config(
            db,
            base_url=body.base_url,
            api_key=body.api_key,
            model=body.model,
            actor=f"user:{username}",
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _view(cfg.base_url, cfg.model, cfg.api_key, "db", True)


@router.post("/llm/test", summary="测试 AI 连接（发一次最小请求）")
async def test_llm(
    body: LlmConfigBody,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_user),
):
    """api_key 留空时用已保存的（或 .env 兜底的）key 测试当前配置。"""
    key = body.api_key.strip()
    base_url = body.base_url.strip() or settings.llm_base_url
    model = body.model.strip() or settings.llm_model
    if not key:
        cfg = await get_llm_config(db)
        key = cfg.api_key if cfg is not None else settings.llm_api_key
    if not key:
        raise HTTPException(status_code=422, detail="尚未配置 api_key")
    client = LLMClient(
        base_url=base_url, api_key=key, model=model, timeout=settings.llm_timeout_seconds
    )
    try:
        resp = await client.chat([{"role": "user", "content": "ping，请只回复 pong"}])
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"[:300]}
    return {"ok": True, "message": f"连接成功：{resp.content or '(空回复)'}"}
