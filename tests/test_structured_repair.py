"""S9 结构化输出 feedback 修复开关：默认关闭零成本、开启后有界重生成、超预算 429。"""

import asyncio

import pytest

from app.core.audit import InMemoryAuditStore, RoutingDecisionLog
from app.core.config import AppConfig
from app.core.errors import GatewayError
from app.core.health import HealthTracker
from app.core.limits import CapacityGuard
from app.core.metrics import InMemoryMetricsRecorder
from app.core.prompts import PromptEngine, PromptRegistry
from app.core.providers.base import ProviderResult
from app.core.router import ModelRouter
from app.core.schemas import LLMRequest, Message, Usage
from app.core.validation import repair_instruction
from app.service.llm_service import LLMService

MODELS = {
    "chat-fast": {
        "adapter": "openai_compatible",
        "capabilities": ["streaming", "structured_output"],
        "priority": 100,
        "provider_model": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "structured_output_mode": "json_object",
        "pricing": {"input": 1.0, "cached_input": 0.1, "output": 4.0},
    },
}

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def make_config(**overrides) -> AppConfig:
    raw = {"version": "1.0", "models": MODELS}
    raw.update(overrides)
    return AppConfig.model_validate(raw)


class SequenceProvider:
    """按顺序出内容的 Fake：第一次返回首项，之后每次弹出下一项。"""

    capabilities = {"streaming", "structured_output"}

    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.complete_calls = 0
        self.last_messages: list[Message] | None = None

    async def complete(self, config, call) -> ProviderResult:

        self.complete_calls += 1
        self.last_messages = list(call.messages)
        content = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        return ProviderResult(
            content=content,
            usage=Usage(input_tokens=10, output_tokens=5),
            finish_reason="stop",
            provider_request_id="up-1",
        )

    async def stream(self, config, call):
        raise NotImplementedError

    async def close(self) -> None:
        pass


def _service(config, provider, clock=None):
    return LLMService(
        config=config,
        router=ModelRouter(),
        health=HealthTracker(
            consecutive_failure_threshold=3, cooldown_seconds=30, clock=lambda: 0.0
        ),
        capacity=CapacityGuard(max_concurrent_requests=100, per_model_max_concurrent=20),
        prompts=PromptEngine(PromptRegistry()),
        audit=InMemoryAuditStore(),
        decision_log=RoutingDecisionLog(),
        metrics=InMemoryMetricsRecorder(),
        sleep=asyncio.sleep,
        clock=clock or (lambda: 0.0),
        providers={"openai_compatible": provider},
    )


def _request(timeout_seconds: float = 30) -> LLMRequest:
    return LLMRequest(
        messages=[Message(role="user", content="hi")],
        response_schema=SCHEMA,
        timeout_seconds=timeout_seconds,
    )


class MutableClock:
    """可推进的假时钟：测试中模拟时间流逝。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.mark.asyncio
async def test_repair_disabled_keeps_zero_cost_behavior() -> None:
    # 默认关闭：失败不重生成，直接 schema_validation_failed。
    provider = SequenceProvider(['{"answer": 1}'])
    audit = InMemoryAuditStore()
    service = _service(make_config(), provider)
    service.audit = audit

    with pytest.raises(GatewayError) as exc_info:
        await service.complete(_request())
    assert exc_info.value.code == "schema_validation_failed"
    assert provider.complete_calls == 1
    trace = (await audit.list())[0]
    assert trace.validation_result == "failed"
    assert trace.repair_attempts == 0


@pytest.mark.asyncio
async def test_repair_enabled_succeeds_after_one_retry() -> None:
    config = make_config(
        structured_output={"repair": {"enabled": True, "max_llm_repair_attempts": 1}}
    )
    provider = SequenceProvider(['{"answer": 1}', '{"answer": "ok"}'])
    audit = InMemoryAuditStore()
    service = _service(config, provider)
    service.audit = audit

    response = await service.complete(_request())
    assert response.parsed == {"answer": "ok"}
    assert provider.complete_calls == 2
    # 修复请求把失败输出 + 修复指令作为新消息追加。
    assert provider.last_messages[-2].role == "assistant"
    assert "Schema" in provider.last_messages[-1].content
    trace = (await audit.list())[0]
    assert trace.validation_result == "passed"
    assert trace.repair_attempts == 1


@pytest.mark.asyncio
async def test_repair_exhausted_raises_validation_error() -> None:
    config = make_config(
        structured_output={"repair": {"enabled": True, "max_llm_repair_attempts": 1}}
    )
    provider = SequenceProvider(['{"answer": 1}', '{"answer": 2}'])
    audit = InMemoryAuditStore()
    service = _service(config, provider)
    service.audit = audit

    with pytest.raises(GatewayError) as exc_info:
        await service.complete(_request())
    assert exc_info.value.code == "schema_validation_failed"
    assert provider.complete_calls == 2
    trace = (await audit.list())[0]
    assert trace.validation_result == "failed"
    assert trace.repair_attempts == 1


@pytest.mark.asyncio
async def test_repair_over_budget_raises_429() -> None:
    config = make_config(
        structured_output={"repair": {"enabled": True, "max_llm_repair_attempts": 1}}
    )
    clock = MutableClock()
    provider = SequenceProvider(['{"answer": 1}', '{"answer": "ok"}'])
    audit = InMemoryAuditStore()
    service = _service(config, provider, clock=clock)
    service.audit = audit
    service._config = service._config.model_copy(
        update={
            "limits": service._config.limits.model_copy(
                update={"min_remaining_budget_seconds": 100.0}
            )
        }
    )
    # 首次调用消耗 30s：剩余 90s 预算不足，修复调用发起前即 429。
    original_complete = provider.complete

    async def complete_then_advance(config, call):
        result = await original_complete(config, call)
        clock.t = 30.0
        return result

    provider.complete = complete_then_advance

    with pytest.raises(GatewayError) as exc_info:
        await service.complete(_request(timeout_seconds=120))
    assert exc_info.value.code == "insufficient_time_budget"
    assert exc_info.value.status_code == 429
    assert provider.complete_calls == 1  # 未触发修复调用
    assert (await audit.list())[0].repair_attempts == 0


def test_repair_instruction_contains_error_and_schema() -> None:
    error = GatewayError("schema_validation_failed", "模型结果不符合 response_schema", 502)
    instruction = repair_instruction(error, SCHEMA)
    assert "模型结果不符合 response_schema" in instruction
    assert '"answer"' in instruction
    assert "Markdown" in instruction
