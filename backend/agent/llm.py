"""LLM 适配层（architecture.md §7.3）：httpx 直调 OpenAI 兼容 /chat/completions。

不引入 langchain/openai SDK——切供应商只改 .env 的 LLM_BASE_URL/LLM_API_KEY/
LLM_MODEL。重试语义（design.md §5.4）：单次请求 30s 超时，失败重试 1 次；
structured() 输出校验失败带错误信息重试 1 次，再失败抛 LLMError（由调用节点
上抛，graph 捕获后置 failed）。
"""
import json
import re
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from core.config import settings

T = TypeVar("T", bound=BaseModel)

# 429/5xx/超时类可重试；其余 4xx（配额耗尽 401/403 等）重试无意义，直接抛
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """LLM 调用最终失败（重试后仍失败，或不可重试的客户端错误）。"""


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class LLMResponse(BaseModel):
    content: str | None
    tool_calls: list[ToolCall] = []


def _extract_json(text: str) -> Any:
    """容错解析模型输出：剥掉 markdown 代码围栏，失败时截取首尾大括号之间。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```\s*$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if 0 <= start < end:
            return json.loads(stripped[start : end + 1])
        raise


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    async def chat(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> LLMResponse:
        """对话补全，带 function calling 的 tool_calls 解析（§7.3）。"""
        payload: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            payload["tools"] = tools
        data = await self._request(payload)
        message = data["choices"][0].get("message") or {}
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except (TypeError, json.JSONDecodeError):
                raise LLMError(f"tool_call {fn.get('name')!r} 的 arguments 不是合法 JSON")
            if not isinstance(arguments, dict):
                raise LLMError(f"tool_call {fn.get('name')!r} 的 arguments 不是 JSON 对象")
            calls.append(
                ToolCall(id=raw.get("id") or "", name=fn.get("name") or "", arguments=arguments)
            )
        return LLMResponse(content=message.get("content"), tool_calls=calls)

    async def structured(self, messages: list[dict], schema: type[T]) -> T:
        """结构化输出（RCA/Plan 生成用，§7.3）。

        JSON Schema 注入 system 提示词，不做 response_format 强约束（供应商
        兼容性）；校验失败把错误信息回传重试一次（§5.4），再失败抛 LLMError。
        """
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        convo: list[dict] = [
            *messages,
            {
                "role": "system",
                "content": (
                    "你是 JSON 生成器。只输出一个符合以下 JSON Schema 的 JSON 对象，"
                    "禁止输出解释、markdown 代码块或任何其他文本：\n" + schema_json
                ),
            },
        ]
        last_error = ""
        for _ in range(2):
            resp = await self.chat(convo)
            text = resp.content or ""
            try:
                return schema.model_validate(_extract_json(text))
            except (ValueError, ValidationError) as e:
                last_error = str(e)[:500]
                convo = [
                    *convo,
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": (
                            f"你的输出不符合 JSON Schema：{last_error}\n"
                            "请重新只输出符合 Schema 的 JSON 对象。"
                        ),
                    },
                ]
        raise LLMError(f"structured 两次输出均不合规：{last_error}")

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """带 §5.4 重试语义的 /chat/completions 调用，返回含 choices 的响应体。"""
        headers = {"Authorization": f"Bearer {self._api_key}"}
        url = f"{self._base_url}/chat/completions"
        last_error = ""
        for _ in range(2):
            try:
                async with httpx.AsyncClient(timeout=float(self._timeout)) as client:
                    resp = await client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as e:
                last_error = f"网络/超时错误：{e!r}"
                continue
            if resp.status_code in _RETRYABLE_STATUS:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                continue
            if resp.status_code >= 400:
                raise LLMError(f"LLM 请求失败 HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                data = resp.json()
            except ValueError:
                last_error = f"响应不是 JSON：{resp.text[:200]}"
                continue
            if not data.get("choices"):
                last_error = f"响应缺少 choices：{json.dumps(data, ensure_ascii=False)[:300]}"
                continue
            return data
        raise LLMError(f"LLM 调用重试后仍失败：{last_error}")


llm = LLMClient(
    base_url=settings.llm_base_url,
    api_key=settings.llm_api_key,
    model=settings.llm_model,
    timeout=settings.llm_timeout_seconds,
)
