"""OpenAI Compatible 适配器：chat_completion 协议、SDK 异常翻译、客户端连接池缓存。

将统一请求转换为 OpenAI Compatible 调用，隔离厂商协议差异；API Key 保留在
Gateway 进程内，业务 Agent 无需接触供应商密钥。客户端按 (base_url, api_key_env)
键控缓存复用，修复基线"每次调用新建客户端"的缺陷。
"""

import json
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)

from app.core.capabilities import Capability
from app.core.config import ModelConfig
from app.core.errors import GatewayError
from app.core.providers.base import (
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderCall,
    ProviderConnectionError,
    ProviderInvalidResponseError,
    ProviderRateLimitError,
    ProviderResult,
    ProviderServerError,
    ProviderTimeoutError,
    StreamChunk,
)
from app.core.schemas import Usage


def _translate_status(exc: APIStatusError) -> GatewayError:
    status = exc.status_code
    if status in (401, 403):
        return ProviderAuthError()
    if 500 <= status < 600:
        return ProviderServerError()
    return ProviderBadRequestError()


def _retry_after(exc: APIStatusError) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", {})
    raw = headers.get("retry-after")
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


def _translate_sdk_error(exc: Exception) -> GatewayError:
    if isinstance(exc, RateLimitError):
        return ProviderRateLimitError(retry_after_seconds=_retry_after(exc))
    if isinstance(exc, APITimeoutError):
        return ProviderTimeoutError()
    if isinstance(exc, APIConnectionError):
        return ProviderConnectionError()
    if isinstance(exc, APIStatusError):
        return _translate_status(exc)
    return GatewayError("upstream_error", f"未分类的上游错误: {exc}")


class OpenAICompatibleProvider:
    capabilities = {Capability.STREAMING, Capability.STRUCTURED_OUTPUT}

    def __init__(
        self, client_factory: Callable[[ModelConfig], AsyncOpenAI] | None = None
    ) -> None:
        self._client_factory = client_factory
        self._clients: dict[tuple[str, str], AsyncOpenAI] = {}

    def _default_factory(self, config: ModelConfig) -> AsyncOpenAI:
        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise GatewayError("gateway_misconfigured", "Gateway 模型凭据未配置", 503)
        return AsyncOpenAI(api_key=api_key, base_url=config.base_url, max_retries=0)

    def get_client(self, config: ModelConfig) -> AsyncOpenAI:
        # 惰性创建 + 按 (base_url, api_key_env) 缓存复用 → 共享 httpx 连接池；
        # 单事件循环内 dict 缓存无锁安全（asyncio 单线程模型）。
        key = (config.base_url, config.api_key_env)
        if key not in self._clients:
            factory = self._client_factory or self._default_factory
            self._clients[key] = factory(config)
        return self._clients[key]

    async def close(self) -> None:
        # 释放连接池，app lifespan 在进程退出时调用。
        for client in self._clients.values():
            await client.close()
        self._clients.clear()

    async def complete(self, config: ModelConfig, call: ProviderCall) -> ProviderResult:
        client = self.get_client(config)
        request_data: dict[str, Any] = {
            "model": config.provider_model,
            "messages": [message.model_dump() for message in call.messages],
            "timeout": call.timeout_seconds,
        }
        if call.response_schema is not None:
            if config.structured_output_mode == "json_schema":
                request_data["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "agent_response",
                        "strict": True,
                        "schema": call.response_schema,
                    },
                }
            else:
                request_data["response_format"] = {"type": "json_object"}
                request_data["messages"] = [
                    {
                        "role": "system",
                        "content": (
                            "只返回一个合法 JSON 对象，必须严格符合下列 JSON Schema，"
                            "不要返回 Markdown 或额外文字："
                            f"{json.dumps(call.response_schema, ensure_ascii=False)}"
                        ),
                    },
                    *request_data["messages"],
                ]
        try:
            completion = await client.chat.completions.create(**request_data)
        except Exception as exc:  # noqa: BLE001 —— SDK 异常层级翻译
            raise _translate_sdk_error(exc) from exc
        if not completion.choices:
            # L3 协议检查：choices 空
            raise ProviderInvalidResponseError("provider_invalid_response", "上游响应缺少 choices")
        choice = completion.choices[0]
        message = choice.message
        usage = completion.usage
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        return ProviderResult(
            content=(message.content if message else None) or "",
            usage=Usage(
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
                cached_input_tokens=(
                    prompt_details.cached_tokens if prompt_details else 0
                ),
            ),
            finish_reason=choice.finish_reason,
            provider_request_id=completion.id,
        )

    async def stream(self, config: ModelConfig, call: ProviderCall) -> AsyncIterator[StreamChunk]:
        client = self.get_client(config)
        try:
            response = await client.chat.completions.create(
                model=config.provider_model,
                messages=[message.model_dump() for message in call.messages],
                stream=True,
                timeout=call.timeout_seconds,
                stream_options={"include_usage": True},
            )
            async for chunk in response:
                usage = chunk.usage
                if usage is not None:
                    # 末块 usage chunk：无内容增量，只携带真实 Token 统计。
                    prompt_details = getattr(usage, "prompt_tokens_details", None)
                    yield StreamChunk(
                        text=None,
                        usage=Usage(
                            input_tokens=usage.prompt_tokens or 0,
                            output_tokens=usage.completion_tokens or 0,
                            cached_input_tokens=(
                                prompt_details.cached_tokens if prompt_details else 0
                            ),
                        ),
                    )
                    continue
                choice = chunk.choices[0] if chunk.choices else None
                if choice and choice.delta and choice.delta.content:
                    yield StreamChunk(text=choice.delta.content, usage=None)
        except Exception as exc:  # noqa: BLE001
            raise _translate_sdk_error(exc) from exc
