"""① Adapter Contract：请求/响应/异常翻译、客户端缓存复用、close 释放。"""

import json

import httpx
import pytest
from openai import AsyncOpenAI

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
    ProviderServerError,
    ProviderTimeoutError,
)
from app.core.providers.openai_compatible import OpenAICompatibleProvider
from app.core.schemas import Message


def make_client(handler, base_url="https://api.test"):
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url=base_url)
    return AsyncOpenAI(api_key="test-key", base_url=base_url, http_client=http_client, max_retries=0)


def make_provider(handler, base_url="https://api.test"):
    return OpenAICompatibleProvider(
        client_factory=lambda config: make_client(handler, base_url=config.base_url)
    )


CONFIG = ModelConfig(
    adapter="openai_compatible",
    capabilities={Capability.STREAMING, Capability.STRUCTURED_OUTPUT},
    priority=100,
    provider_model="deepseek-v4-flash",
    base_url="https://api.test",
    api_key_env="DEEPSEEK_API_KEY",
    structured_output_mode="json_object",
    pricing={"input": 1.0, "cached_input": 0.1, "output": 4.0},
)

CALL = ProviderCall(
    messages=[Message(role="user", content="你好")],
    timeout_seconds=30,
    response_schema=None,
)


def _completion_json(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "created": 123,
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
        request=request,
    )


@pytest.mark.asyncio
async def test_complete_translates_request_and_response() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/chat/completions"
        captured["body"] = json.loads(request.content)
        return _completion_json(request)

    provider = make_provider(handler)
    result = await provider.complete(CONFIG, CALL)
    assert captured["body"]["model"] == "deepseek-v4-flash"
    assert captured["body"]["messages"] == [{"role": "user", "content": "你好"}]
    assert result.content == "hi"
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5
    assert result.finish_reason == "stop"
    assert result.provider_request_id == "chatcmpl-123"
    await provider.close()


@pytest.mark.asyncio
async def test_json_schema_mode_sends_native_response_format() -> None:
    captured: dict = {}
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _completion_json(request)

    provider = make_provider(handler)
    config = CONFIG.model_copy(update={"structured_output_mode": "json_schema"})
    await provider.complete(
        config, ProviderCall(messages=CALL.messages, timeout_seconds=30, response_schema=schema)
    )
    rf = captured["body"]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] == schema
    await provider.close()


@pytest.mark.asyncio
async def test_json_object_mode_injects_schema_into_system_message() -> None:
    captured: dict = {}
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _completion_json(request)

    provider = make_provider(handler)
    await provider.complete(
        CONFIG, ProviderCall(messages=CALL.messages, timeout_seconds=30, response_schema=schema)
    )
    body = captured["body"]
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"
    assert "JSON Schema" in body["messages"][0]["content"]
    await provider.close()


@pytest.mark.asyncio
async def test_rate_limit_translated_with_retry_after() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "slow down", "type": "rate_limit_error"}},
            headers={"retry-after": "3"},
            request=request,
        )

    provider = make_provider(handler)
    with pytest.raises(ProviderRateLimitError) as exc_info:
        await provider.complete(CONFIG, CALL)
    assert exc_info.value.code == "provider_rate_limited"
    assert exc_info.value.retry_after_seconds == 3
    await provider.close()


@pytest.mark.asyncio
async def test_5xx_translated_to_server_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {}}, request=request)

    provider = make_provider(handler)
    with pytest.raises(ProviderServerError):
        await provider.complete(CONFIG, CALL)
    await provider.close()


@pytest.mark.asyncio
async def test_401_translated_to_auth_error_503() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {}}, request=request)

    provider = make_provider(handler)
    with pytest.raises(ProviderAuthError) as exc_info:
        await provider.complete(CONFIG, CALL)
    assert exc_info.value.status_code == 503
    await provider.close()


@pytest.mark.asyncio
async def test_400_translated_to_bad_request() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {}}, request=request)

    provider = make_provider(handler)
    with pytest.raises(ProviderBadRequestError):
        await provider.complete(CONFIG, CALL)
    await provider.close()


@pytest.mark.asyncio
async def test_connect_error_translated() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = make_provider(handler)
    with pytest.raises(ProviderConnectionError):
        await provider.complete(CONFIG, CALL)
    await provider.close()


@pytest.mark.asyncio
async def test_read_timeout_translated() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = make_provider(handler)
    with pytest.raises(ProviderTimeoutError):
        await provider.complete(CONFIG, CALL)
    await provider.close()


@pytest.mark.asyncio
async def test_empty_choices_is_l3_invalid_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-123",
                "object": "chat.completion",
                "choices": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
            },
            request=request,
        )

    provider = make_provider(handler)
    with pytest.raises(ProviderInvalidResponseError):
        await provider.complete(CONFIG, CALL)
    await provider.close()


@pytest.mark.asyncio
async def test_same_config_reuses_single_client() -> None:
    factory_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        return _completion_json(request)

    def factory(config):
        nonlocal factory_calls
        factory_calls += 1
        return make_client(handler)

    provider = OpenAICompatibleProvider(client_factory=factory)
    await provider.complete(CONFIG, CALL)
    await provider.complete(CONFIG, CALL)
    assert factory_calls == 1
    await provider.close()
    assert provider._clients == {}


@pytest.mark.asyncio
async def test_stream_yields_only_non_empty_deltas() -> None:
    stream_body = (
        'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,'
        '"delta":{"content":"你"},"finish_reason":null}]}\n\n'
        'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,'
        '"delta":{},"finish_reason":null}]}\n\n'
        'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,'
        '"delta":{"content":"好"},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=stream_body,
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    provider = make_provider(handler)
    chunks = [chunk async for chunk in provider.stream(CONFIG, CALL)]
    assert [chunk.text for chunk in chunks] == ["你", "好"]
    assert all(chunk.usage is None for chunk in chunks)
    await provider.close()


@pytest.mark.asyncio
async def test_missing_api_key_raises_gateway_misconfigured(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    provider = OpenAICompatibleProvider()  # 默认工厂，运行时读 env
    with pytest.raises(GatewayError) as exc_info:
        await provider.complete(CONFIG, CALL)
    assert exc_info.value.code == "gateway_misconfigured"
    assert exc_info.value.status_code == 503
    await provider.close()


def test_provider_declares_capabilities() -> None:
    assert OpenAICompatibleProvider.capabilities == {
        Capability.STREAMING,
        Capability.STRUCTURED_OUTPUT,
    }
