"""S5 流式真实 Token 统计：StreamChunk 协议、末块 usage 解析、估算兜底标记。"""

import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from app.core.audit import InMemoryAuditStore, RoutingDecisionLog
from app.core.config import ModelConfig
from app.core.health import HealthTracker
from app.core.limits import CapacityGuard
from app.core.metrics import InMemoryMetricsRecorder
from app.core.prompts import PromptEngine, PromptRegistry
from app.core.providers.base import ProviderCall, StreamChunk
from app.core.providers.openai_compatible import OpenAICompatibleProvider
from app.core.router import ModelRouter
from app.core.schemas import LLMRequest, Message, StreamUsageEvent, Usage
from app.service.llm_service import LLMService

CONFIG = ModelConfig(
    adapter="openai_compatible",
    capabilities={"streaming", "structured_output"},
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


@pytest.mark.asyncio
async def test_provider_stream_parses_final_usage_chunk() -> None:
    """末块 usage chunk → StreamChunk(text=None, usage=...)；请求携带 stream_options。"""
    stream_body = (
        'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,'
        '"delta":{"content":"你"},"finish_reason":null}]}\n\n'
        'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,'
        '"delta":{"content":"好"},"finish_reason":null}]}\n\n'
        'data: {"id":"c1","object":"chat.completion.chunk","choices":[],'
        '"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}\n\n'
        "data: [DONE]\n\n"
    )
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=stream_body,
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.test")
    client = AsyncOpenAI(
        api_key="test-key", base_url="https://api.test", http_client=http_client, max_retries=0
    )
    provider = OpenAICompatibleProvider(client_factory=lambda config: client)

    chunks = [chunk async for chunk in provider.stream(CONFIG, CALL)]
    assert captured["body"]["stream_options"] == {"include_usage": True}
    assert chunks == [
        StreamChunk(text="你", usage=None),
        StreamChunk(text="好", usage=None),
        StreamChunk(text=None, usage=Usage(input_tokens=10, output_tokens=2)),
    ]
    await provider.close()


class UsageStreamProvider:
    """流式 Fake：可选末块 usage；有 usage 表示上游支持真实统计。"""

    capabilities = {"streaming", "structured_output"}

    def __init__(self, usage: Usage | None = None) -> None:
        self.usage = usage

    async def complete(self, config, call):
        raise NotImplementedError

    async def stream(self, config, call):
        yield StreamChunk(text="你", usage=None)
        yield StreamChunk(text="好", usage=None)
        if self.usage is not None:
            yield StreamChunk(text=None, usage=self.usage)

    async def close(self) -> None:
        pass


def _service(config, provider, audit) -> LLMService:
    return LLMService(
        config=config,
        router=ModelRouter(),
        health=HealthTracker(
            consecutive_failure_threshold=3, cooldown_seconds=30, clock=lambda: 0.0
        ),
        capacity=CapacityGuard(max_concurrent_requests=100, per_model_max_concurrent=20),
        prompts=PromptEngine(PromptRegistry()),
        audit=audit,
        decision_log=RoutingDecisionLog(),
        metrics=InMemoryMetricsRecorder(),
        sleep=asyncio.sleep,
        clock=lambda: 0.0,
        providers={"openai_compatible": provider},
    )


def _request(model: str | None = None) -> LLMRequest:
    return LLMRequest(
        model=model,
        messages=[Message(role="user", content="hi")],
        timeout_seconds=30,
    )


@pytest.mark.asyncio
async def test_service_stream_uses_real_usage_and_marks_not_estimated(sample_config) -> None:
    real = Usage(input_tokens=321, output_tokens=654)
    audit = InMemoryAuditStore()
    service = _service(sample_config, UsageStreamProvider(usage=real), audit)

    events = [event async for event in service.stream(_request(model="chat-fast"))]

    usage_events = [e for e in events if isinstance(e, StreamUsageEvent)]
    assert len(usage_events) == 1
    assert usage_events[0].usage == real
    assert usage_events[0].estimated is False
    trace = (await audit.list())[0]
    assert trace.usage == real
    assert trace.usage_estimated is False


@pytest.mark.asyncio
async def test_service_stream_falls_back_to_estimate_when_no_usage(sample_config) -> None:
    audit = InMemoryAuditStore()
    service = _service(sample_config, UsageStreamProvider(usage=None), audit)

    events = [event async for event in service.stream(_request(model="chat-fast"))]

    usage_events = [e for e in events if isinstance(e, StreamUsageEvent)]
    assert len(usage_events) == 1
    assert usage_events[0].estimated is True
    assert usage_events[0].usage.input_tokens >= 0
    trace = (await audit.list())[0]
    assert trace.usage_estimated is True
