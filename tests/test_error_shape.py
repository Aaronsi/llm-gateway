"""S14 错误响应形状开关：api.error_shape 切换 gateway/openai 两种错误响应形状。

- gateway（默认）：{"detail": {"code", "message"}}（行为不变）。
- openai：{"error": {"message", "type", "param", "code"}}，稳定 code 映射到 type。
- facade 流式错误（openai 形状）：data: {"error": ...} + data: [DONE]。
"""

import json

import pytest
from pydantic import ValidationError

from app.api.errors import to_json_response
from app.api.streaming import format_openai_chunks
from app.core.config import AppConfig
from app.core.errors import GatewayError
from app.core.providers.base import ProviderConnectionError, StreamChunk
from app.core.schemas import StreamDelta, StreamFailed


def _openai_shape_config(sample_config) -> AppConfig:
    # model_copy(update=dict) 不会把嵌套 dict 转换成 ApiConfig，需改字段后赋值。
    config = sample_config.model_copy(deep=True)
    config.api.error_shape = "openai"
    return config


def test_default_shape_is_gateway() -> None:
    response = to_json_response(GatewayError("unknown_model", "不存在", 400))
    assert response.status_code == 400
    assert json.loads(response.body) == {"detail": {"code": "unknown_model", "message": "不存在"}}


def test_openai_shape_payload_maps_code_to_type() -> None:
    response = to_json_response(GatewayError("unknown_model", "不存在", 400), "openai")
    assert response.status_code == 400
    assert json.loads(response.body) == {
        "error": {"message": "不存在", "type": "unknown_model", "param": None, "code": None}
    }


def test_error_shape_invalid_value_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AppConfig.model_validate(
            {
                "version": "1.0",
                "models": {},
                "api": {"error_shape": "weird"},
                "limits": {},
                "retry": {},
                "prompts": {},
                "admin_endpoints": {},
                "logging": {},
            }
        )
    assert "error_shape" in str(exc_info.value)


@pytest.mark.asyncio
async def test_openai_error_shape_via_api(make_client, sample_config) -> None:
    config = _openai_shape_config(sample_config)
    async with make_client(config=config) as client:
        response = await client.post(
            "/v1/llm",
            json={"model": "ghost", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 400
    body = response.json()
    assert "detail" not in body
    assert body["error"]["type"] == "unknown_model"
    assert body["error"]["param"] is None


def test_openai_stream_error_emits_error_event_then_done() -> None:
    events = [
        StreamDelta(type="delta", text="hi"),
        StreamFailed(type="failed", error_code="upstream_stream_failed", message="上游流中断"),
    ]
    chunks = format_openai_chunks(
        events, call_id="call-1", model="chat-fast", error_shape="openai"
    )
    assert chunks[-1] == "data: [DONE]\n\n"
    error_payload = json.loads(chunks[-2].removeprefix("data: "))
    assert error_payload == {
        "error": {
            "message": "上游流中断",
            "type": "upstream_stream_failed",
            "param": None,
            "code": None,
        }
    }


def test_gateway_stream_error_truncates_without_error_event() -> None:
    events = [
        StreamDelta(type="delta", text="hi"),
        StreamFailed(type="failed", error_code="upstream_stream_failed", message="上游流中断"),
    ]
    chunks = format_openai_chunks(events, call_id="call-1", model="chat-fast")
    assert chunks[-1] != "data: [DONE]\n\n"
    assert len(chunks) == 1  # 只断流，不追加错误事件


class FailingMidStreamProvider:
    capabilities = {"streaming", "structured_output"}

    async def complete(self, config, call):
        raise NotImplementedError

    async def stream(self, config, call):
        yield StreamChunk(text="首块", usage=None)
        raise ProviderConnectionError()

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_facade_stream_error_via_api_uses_openai_shape(make_client, sample_config) -> None:
    config = _openai_shape_config(sample_config)
    async with make_client(config=config, provider=FailingMidStreamProvider()) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "chat-fast", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200, response.text
    assert '"error"' in response.text
    assert "data: [DONE]" in response.text
    assert '"type": "upstream_stream_failed"' in response.text
