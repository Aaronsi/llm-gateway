"""OpenAI 兼容 facade：/v1/chat/completions 进出翻译、流式 chunk、扩展字段接受后忽略。

协议分离只在边界发生：response_format → response_schema（to_internal），
统一响应 → chat.completion（to_openai_response）；model 字段 = 平台逻辑模型名。
"""

import json
import logging

import pytest

from app.api.schemas_openai import ChatCompletionRequest, to_openai_response
from app.core.providers.base import ProviderResult
from app.core.schemas import LatencyBreakdown, LLMResponse, Usage


class FakeProvider:
    capabilities = {"streaming", "structured_output"}

    def __init__(self, content: str = "ok") -> None:
        self.content = content

    async def complete(self, config, call) -> ProviderResult:
        return ProviderResult(
            content=self.content,
            usage=Usage(input_tokens=10, output_tokens=5),
            finish_reason="stop",
            provider_request_id="up-1",
        )

    async def stream(self, config, call):
        yield "你"
        yield "好"

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_openai_chat_completion_basic(make_client) -> None:
    async with make_client() as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "chat-fast", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "chat-fast"
    assert body["choices"][0]["message"]["content"] == "ok"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert response.headers["x-request-id"] == body["id"]
    assert response.headers["x-trace-id"]


@pytest.mark.asyncio
async def test_openai_accepts_and_ignores_extra_fields(make_client) -> None:
    # SDK 常用扩展字段：接受后忽略，保证任意 OpenAI SDK 调用不炸。
    async with make_client() as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "chat-fast",
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": 0.7,
                "top_p": 0.9,
                "n": 1,
                "stop": None,
                "seed": 42,
                "user": "u1",
            },
        )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_openai_json_schema_response_format(make_client) -> None:
    provider = FakeProvider(content=json.dumps({"answer": "ok"}))
    async with make_client(provider=provider) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "chat-fast",
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "answer_schema",
                        "schema": {
                            "type": "object",
                            "properties": {"answer": {"type": "string"}},
                            "required": ["answer"],
                            "additionalProperties": False,
                        },
                    },
                },
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == json.dumps({"answer": "ok"})


@pytest.mark.asyncio
async def test_openai_json_object_response_format_invalid_502(make_client) -> None:
    # json_object → 本地 L4 校验必须输出 JSON 对象；默认 content="ok" 非法。
    async with make_client() as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "chat-fast",
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": {"type": "json_object"},
            },
        )
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "invalid_json"


@pytest.mark.asyncio
async def test_openai_streaming_chunks(make_client) -> None:
    async with make_client() as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "chat-fast", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"object": "chat.completion.chunk"' in response.text
    assert "data: [DONE]" in response.text
    assert '"finish_reason": "stop"' in response.text
    assert '"usage"' in response.text


@pytest.mark.asyncio
async def test_openai_unknown_model_400(make_client) -> None:
    async with make_client() as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "ghost", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unknown_model"


def test_to_internal_maps_json_schema_response_format() -> None:
    request = ChatCompletionRequest.model_validate(
        {
            "model": "chat-fast",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "s", "schema": {"type": "object"}},
            },
        }
    )
    internal = request.to_internal()
    assert internal.model == "chat-fast"
    assert internal.response_schema == {"type": "object"}
    assert internal.stream is False
    assert internal.messages[0].role == "user"
    assert internal.messages[0].content == "hi"


def test_to_internal_maps_json_object_to_object_schema() -> None:
    request = ChatCompletionRequest.model_validate(
        {
            "model": "chat-fast",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_object"},
        }
    )
    internal = request.to_internal()
    assert internal.response_schema == {"type": "object"}


def test_to_openai_response_shape() -> None:
    internal = LLMResponse(
        call_id="c1",
        model="chat-fast",
        content="hello",
        usage=Usage(input_tokens=10, output_tokens=5),
        latency=LatencyBreakdown(),
        attempts=1,
    )
    payload = to_openai_response(internal)
    assert payload["id"] == "c1"
    assert payload["object"] == "chat.completion"
    assert payload["model"] == "chat-fast"
    assert payload["choices"][0]["message"] == {"role": "assistant", "content": "hello"}
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_to_openai_response_includes_ignored_fields_when_present() -> None:
    internal = LLMResponse(
        call_id="c1",
        model="chat-fast",
        content="hello",
        usage=Usage(),
        latency=LatencyBreakdown(),
        attempts=1,
    )
    payload = to_openai_response(internal, ignored_fields=["temperature", "top_p"])
    assert payload["ignored_fields"] == ["temperature", "top_p"]


def test_to_openai_response_omits_ignored_fields_when_empty() -> None:
    internal = LLMResponse(
        call_id="c1",
        model="chat-fast",
        content="hello",
        usage=Usage(),
        latency=LatencyBreakdown(),
        attempts=1,
    )
    payload = to_openai_response(internal, ignored_fields=[])
    assert "ignored_fields" not in payload


def test_request_exposes_ignored_fields_sorted() -> None:
    request = ChatCompletionRequest.model_validate(
        {
            "model": "chat-fast",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.7,
            "top_p": 0.9,
        }
    )
    assert request.ignored_fields == ["temperature", "top_p"]


def test_to_internal_logs_ignored_fields_as_warning(caplog) -> None:
    request = ChatCompletionRequest.model_validate(
        {
            "model": "chat-fast",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.7,
        }
    )
    with caplog.at_level(logging.WARNING, logger="app.access"):
        request.to_internal()
    assert any("temperature" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_openai_extra_fields_reported_in_non_stream_response(make_client) -> None:
    async with make_client() as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "chat-fast",
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": 0.7,
                "seed": 42,
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["ignored_fields"] == ["seed", "temperature"]
