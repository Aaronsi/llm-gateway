"""编排集成：complete/stream、重试/降级、显式模型语义、L4 校验修复、Prompt 引擎、容量守卫。"""

import asyncio

import pytest

from app.core.audit import InMemoryAuditStore, RoutingDecisionLog
from app.core.capabilities import ADAPTER_CAPABILITIES, Capability
from app.core.config import AppConfig
from app.core.errors import GatewayError
from app.core.health import HealthTracker
from app.core.limits import CapacityGuard
from app.core.metrics import InMemoryMetricsRecorder
from app.core.prompts import (
    PromptEngine,
    PromptPackage,
    PromptRegistry,
    PromptVariable,
)
from app.core.providers.base import (
    ProviderConnectionError,
    ProviderResult,
    ProviderTimeoutError,
)
from app.core.router import ModelRouter
from app.core.schemas import (
    LLMRequest,
    Message,
    StreamCompleted,
    StreamDelta,
    StreamFailed,
    Usage,
)
from app.service.llm_service import LLMService


class FakeProvider:
    capabilities = {"streaming", "structured_output"}

    def __init__(
        self,
        content: str = "ok",
        usage: Usage | None = None,
        stream_chunks: list[str] | None = None,
    ) -> None:
        self.content = content
        self.usage = usage or Usage(input_tokens=10, output_tokens=5)
        self.complete_calls = 0
        self.stream_calls = 0
        self.errors_before_success = 0
        self.error = None
        self.stream_chunks: list[str] = stream_chunks if stream_chunks is not None else ["你", "好"]
        self.stream_error = None
        self.stream_usage: Usage | None = None

    async def complete(self, config, call) -> ProviderResult:
        self.complete_calls += 1
        if self.errors_before_success > 0:
            self.errors_before_success -= 1
            raise ProviderTimeoutError()
        if self.error is not None:
            raise self.error
        return ProviderResult(
            content=self.content,
            usage=self.usage,
            finish_reason="stop",
            provider_request_id="up-1",
        )

    async def stream(self, config, call):
        from app.core.providers.base import StreamChunk

        self.stream_calls += 1
        if self.stream_error is not None:
            raise self.stream_error
        for chunk in self.stream_chunks:
            yield StreamChunk(text=chunk, usage=None)
        if self.stream_usage is not None:
            yield StreamChunk(text=None, usage=self.stream_usage)

    async def close(self) -> None:
        pass


def _package(context_budget: int | None = None) -> PromptPackage:
    return PromptPackage(
        name="knowledge_decision",
        version="v1",
        system_template="你是{{ product_name }}的决策器。",
        variables={"product_name": PromptVariable(max_length=50)},
        context_budget=context_budget,
    )


def _engine(**packages: PromptPackage) -> PromptEngine:
    registry = PromptRegistry()
    for package in packages.values():
        registry.register(package)
    return PromptEngine(registry)


@pytest.fixture
def service_bundle(sample_config, fake_sleep):
    health = HealthTracker(consecutive_failure_threshold=3, cooldown_seconds=30, clock=lambda: 0.0)
    audit = InMemoryAuditStore()
    decisions = RoutingDecisionLog()
    metrics = InMemoryMetricsRecorder()

    def build(provider=None, packages=None):
        registry = PromptRegistry()
        for package in (packages or {}).values():
            registry.register(package)
        service = LLMService(
            config=sample_config,
            router=ModelRouter(),
            health=health,
            capacity=CapacityGuard(max_concurrent_requests=100, per_model_max_concurrent=20),
            prompts=PromptEngine(registry),
            audit=audit,
            decision_log=decisions,
            metrics=metrics,
            sleep=fake_sleep,
            clock=lambda: 0.0,
            providers={"openai_compatible": provider or FakeProvider()},
        )
        return service, health, audit, decisions, metrics

    return build


def _request(model: str | None = None, schema=None, prompt=None, timeout: float = 30) -> LLMRequest:
    return LLMRequest(
        model=model,
        messages=[Message(role="user", content="你好")],
        response_schema=schema,
        prompt=prompt,
        timeout_seconds=timeout,
    )


@pytest.mark.asyncio
async def test_complete_returns_response_and_records_trace(service_bundle) -> None:
    service, _, audit, _, metrics = service_bundle()
    response = await service.complete(_request(model="chat-fast"))
    assert response.model == "chat-fast"
    assert response.content == "ok"
    assert response.usage.input_tokens == 10
    assert response.attempts == 1
    traces = await audit.list()
    assert len(traces) == 1
    assert traces[0].status == "success"
    assert traces[0].actual_model == "chat-fast"
    assert traces[0].error_code is None
    assert metrics.snapshot()["requests_total"] == 1


@pytest.mark.asyncio
async def test_retryable_error_is_retried_then_succeeds(service_bundle, fake_sleep) -> None:
    provider = FakeProvider()
    provider.errors_before_success = 1
    service, _, audit, _, _ = service_bundle(provider=provider)
    response = await service.complete(_request(model="chat-fast"))
    assert response.attempts == 2
    assert provider.complete_calls == 2
    # 默认 jitter=true：首次退避 = 0.1 × [0.5, 1.5]，只断言确定性边界。
    assert len(fake_sleep.calls) == 1
    assert 0.05 <= fake_sleep.calls[0] <= 0.15
    assert (await audit.list())[0].resilience.retries == 1


@pytest.mark.asyncio
async def test_fallback_to_next_candidate_when_primary_fails(monkeypatch) -> None:
    # 注册合成 adapter，让 ModelConfig 的能力子集校验通过（adapter 只作寻址键）。
    caps = {Capability.STREAMING, Capability.STRUCTURED_OUTPUT}
    monkeypatch.setitem(ADAPTER_CAPABILITIES, "a1", caps)
    monkeypatch.setitem(ADAPTER_CAPABILITIES, "a2", caps)
    primary = FakeProvider()
    primary.error = ProviderConnectionError()
    backup = FakeProvider(content="backup answer")
    config = AppConfig.model_validate(
        {
            "version": "1.0",
            "models": {
                "m1": {
                    "adapter": "a1",
                    "capabilities": ["streaming", "structured_output"],
                    "priority": 100,
                    "provider_model": "x",
                    "base_url": "https://x",
                    "api_key_env": "K",
                    "pricing": {"input": 1.0, "cached_input": 0.1, "output": 4.0},
                },
                "m2": {
                    "adapter": "a2",
                    "capabilities": ["streaming", "structured_output"],
                    "priority": 80,
                    "provider_model": "y",
                    "base_url": "https://y",
                    "api_key_env": "K",
                    "pricing": {"input": 1.0, "cached_input": 0.1, "output": 4.0},
                },
            },
            "limits": {},
            "retry": {"max_attempts_per_model": 1},
            "prompts": {},
            "admin_endpoints": {},
            "logging": {},
        }
    )
    audit = InMemoryAuditStore()
    service = LLMService(
        config=config,
        router=ModelRouter(),
        health=HealthTracker(consecutive_failure_threshold=3, cooldown_seconds=30, clock=lambda: 0.0),
        capacity=CapacityGuard(100, 20),
        prompts=_engine(),
        audit=audit,
        decision_log=RoutingDecisionLog(),
        metrics=InMemoryMetricsRecorder(),
        sleep=asyncio.sleep,
        clock=lambda: 0.0,
        providers={"a1": primary, "a2": backup},
    )
    response = await service.complete(_request(model="m1"))
    assert response.model == "m2"
    assert response.content == "backup answer"
    trace = (await audit.list())[0]
    assert trace.actual_model == "m2"
    assert trace.resilience.fallbacks == 1


@pytest.mark.asyncio
async def test_unknown_model_raises_400(service_bundle) -> None:
    service, _, audit, _, _ = service_bundle()
    with pytest.raises(GatewayError) as exc_info:
        await service.complete(_request(model="ghost"))
    assert exc_info.value.code == "unknown_model"
    assert exc_info.value.status_code == 400
    assert (await audit.list())[0].status == "failed"


@pytest.mark.asyncio
async def test_l4_validation_failure_is_not_retried_and_not_fallback(service_bundle) -> None:
    provider = FakeProvider(content='{"answer": 1}')
    service, _, audit, _, _ = service_bundle(provider=provider)
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    with pytest.raises(GatewayError) as exc_info:
        await service.complete(_request(model="chat-fast", schema=schema))
    assert exc_info.value.code == "schema_validation_failed"
    assert provider.complete_calls == 1  # 不重试
    assert (await audit.list())[0].status == "failed"


@pytest.mark.asyncio
async def test_l4_repair_fixes_markdown_fence(service_bundle) -> None:
    provider = FakeProvider(content='```json\n{"answer": "ok"}\n```')
    service, _, audit, _, _ = service_bundle(provider=provider)
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    response = await service.complete(_request(model="chat-fast", schema=schema))
    assert response.parsed == {"answer": "ok"}
    assert (await audit.list())[0].validation_result == "passed"


@pytest.mark.asyncio
async def test_all_candidates_unavailable_raises_model_unavailable(service_bundle) -> None:
    provider = FakeProvider()
    provider.error = ProviderConnectionError()
    service, _, audit, _, _ = service_bundle(provider=provider)
    with pytest.raises(GatewayError) as exc_info:
        await service.complete(_request(model="chat-fast"))
    assert exc_info.value.code == "model_unavailable"
    assert exc_info.value.status_code == 502
    assert (await audit.list())[0].status == "failed"


@pytest.mark.asyncio
async def test_prompt_engine_prepends_rendered_system_message(service_bundle) -> None:
    from app.core.schemas import PromptSelection

    service, _, audit, _, _ = service_bundle(packages={("knowledge_decision", "v1"): _package()})
    selection = PromptSelection(
        name="knowledge_decision",
        version="v1",
        variables={"product_name": "差旅助手"},
    )
    response = await service.complete(_request(model="chat-fast", prompt=selection))
    assert response.content == "ok"
    trace = (await audit.list())[0]
    assert trace.prompt_name == "knowledge_decision"
    assert trace.prompt_version == "v1"
    assert trace.prompt_hash  # 非空


@pytest.mark.asyncio
async def test_missing_prompt_variable_raises_400(service_bundle) -> None:
    from app.core.schemas import PromptSelection

    service, _, audit, _, _ = service_bundle(packages={("knowledge_decision", "v1"): _package()})
    selection = PromptSelection(name="knowledge_decision", version="v1", variables={})
    with pytest.raises(GatewayError) as exc_info:
        await service.complete(_request(model="chat-fast", prompt=selection))
    assert exc_info.value.code == "missing_prompt_variable"
    assert exc_info.value.status_code == 400
    assert (await audit.list())[0].status == "failed"


@pytest.mark.asyncio
async def test_unknown_prompt_template_raises_400(service_bundle) -> None:
    from app.core.schemas import PromptSelection

    service, _, _, _, _ = service_bundle()
    selection = PromptSelection(name="nope", version="v1", variables={})
    with pytest.raises(GatewayError) as exc_info:
        await service.complete(_request(model="chat-fast", prompt=selection))
    assert exc_info.value.code == "unknown_prompt_template"


@pytest.mark.asyncio
async def test_context_budget_exceeded_raises_400(service_bundle) -> None:
    from app.core.schemas import PromptSelection

    service, _, _, _, _ = service_bundle(packages={("knowledge_decision", "v1"): _package(context_budget=3)})
    selection = PromptSelection(
        name="knowledge_decision",
        version="v1",
        variables={"product_name": "差旅助手"},
    )
    with pytest.raises(GatewayError) as exc_info:
        await service.complete(_request(model="chat-fast", prompt=selection))
    assert exc_info.value.code == "context_budget_exceeded"


@pytest.mark.asyncio
async def test_control_char_variable_rejected(service_bundle) -> None:
    from app.core.schemas import PromptSelection

    service, _, _, _, _ = service_bundle(packages={("knowledge_decision", "v1"): _package()})
    selection = PromptSelection(name="knowledge_decision", version="v1", variables={"product_name": "a\nb"})
    with pytest.raises(GatewayError) as exc_info:
        await service.complete(_request(model="chat-fast", prompt=selection))
    assert exc_info.value.code == "invalid_prompt_variable"


@pytest.mark.asyncio
async def test_stream_emits_deltas_and_terminal_completed(service_bundle) -> None:
    service, _, audit, _, _ = service_bundle()
    events = [event async for event in service.stream(_request(model="chat-fast"))]
    assert [e.type for e in events] == ["delta", "delta", "usage", "completed"]
    assert isinstance(events[-1], StreamCompleted)
    assert events[-1].model == "chat-fast"
    trace = (await audit.list())[0]
    assert trace.status == "success"
    assert trace.latency.ttft_ms is not None


@pytest.mark.asyncio
async def test_stream_fallback_before_first_delta(monkeypatch) -> None:
    caps = {Capability.STREAMING, Capability.STRUCTURED_OUTPUT}
    monkeypatch.setitem(ADAPTER_CAPABILITIES, "a1", caps)
    monkeypatch.setitem(ADAPTER_CAPABILITIES, "a2", caps)
    config = AppConfig.model_validate(
        {
            "version": "1.0",
            "models": {
                "m1": {
                    "adapter": "a1",
                    "capabilities": ["streaming"],
                    "priority": 100,
                    "provider_model": "x",
                    "base_url": "https://x",
                    "api_key_env": "K",
                    "pricing": {"input": 1.0, "cached_input": 0.1, "output": 4.0},
                },
                "m2": {
                    "adapter": "a2",
                    "capabilities": ["streaming"],
                    "priority": 80,
                    "provider_model": "y",
                    "base_url": "https://y",
                    "api_key_env": "K",
                    "pricing": {"input": 1.0, "cached_input": 0.1, "output": 4.0},
                },
            },
            "limits": {},
            "retry": {"max_attempts_per_model": 1},
            "prompts": {},
            "admin_endpoints": {},
            "logging": {},
        }
    )
    primary = FakeProvider()
    primary.stream_error = ProviderConnectionError()
    backup = FakeProvider(stream_chunks=["备", "用"])
    audit = InMemoryAuditStore()
    service = LLMService(
        config=config,
        router=ModelRouter(),
        health=HealthTracker(consecutive_failure_threshold=3, cooldown_seconds=30, clock=lambda: 0.0),
        capacity=CapacityGuard(100, 20),
        prompts=_engine(),
        audit=audit,
        decision_log=RoutingDecisionLog(),
        metrics=InMemoryMetricsRecorder(),
        sleep=asyncio.sleep,
        clock=lambda: 0.0,
        providers={"a1": primary, "a2": backup},
    )
    events = [event async for event in service.stream(_request(model="m1"))]
    assert isinstance(events[-1], StreamCompleted)
    assert events[-1].model == "m2"
    deltas = [e.text for e in events if isinstance(e, StreamDelta)]
    assert deltas == ["备", "用"]
    assert (await audit.list())[0].actual_model == "m2"


@pytest.mark.asyncio
async def test_stream_error_after_first_delta_emits_failed_terminal(service_bundle) -> None:
    class FailingMidStreamProvider(FakeProvider):
        async def stream(self, config, call):
            from app.core.providers.base import StreamChunk

            self.stream_calls += 1
            yield StreamChunk(text="首块", usage=None)
            raise ProviderConnectionError()

    service, _, audit, _, _ = service_bundle(provider=FailingMidStreamProvider())
    events = [event async for event in service.stream(_request(model="chat-fast"))]
    assert isinstance(events[0], StreamDelta)
    assert isinstance(events[-1], StreamFailed)
    assert events[-1].error_code == "upstream_stream_failed"
    # 终态唯一：failed 之后没有 completed
    assert not any(isinstance(e, StreamCompleted) for e in events)
    assert (await audit.list())[0].status == "failed"


@pytest.mark.asyncio
async def test_pool_full_tries_next_candidate(service_bundle) -> None:
    service, _, audit, _, _ = service_bundle()
    capacity = CapacityGuard(max_concurrent_requests=10, per_model_max_concurrent=1)
    # 占满 chat-fast 的模型池
    assert capacity.acquire("chat-fast")
    service._capacity = capacity
    # chat-fast 池满 → 应路由到 chat-pro
    response = await service.complete(_request(model="chat-fast"))
    assert response.model == "chat-pro"
    assert (await audit.list())[0].actual_model == "chat-pro"
    capacity.release("chat-fast")


@pytest.mark.asyncio
async def test_insufficient_time_budget_raises_429(service_bundle) -> None:
    service, _, _, _, _ = service_bundle()
    service._config = service._config.model_copy(
        update={"limits": service._config.limits.model_copy(update={"min_remaining_budget_seconds": 100.0})}
    )
    with pytest.raises(GatewayError) as exc_info:
        await service.complete(_request(model="chat-fast", timeout=10))
    assert exc_info.value.code == "insufficient_time_budget"
    assert exc_info.value.status_code == 429


def test_prompt_registry_loads_yaml_assets(tmp_path) -> None:
    (tmp_path / "knowledge_decision").mkdir()
    (tmp_path / "knowledge_decision" / "v1.yaml").write_text(
        "name: knowledge_decision\n"
        "version: v1\n"
        "status: published\n"
        "system_template: \"你是{{ product_name }}的决策器。\"\n"
        "variables:\n"
        "  product_name: {type: str, max_length: 50}\n"
        "context_budget: 2000\n",
        encoding="utf-8",
    )
    registry = PromptRegistry.load(tmp_path)
    package = registry.get("knowledge_decision", "v1")
    assert package is not None
    assert package.context_budget == 2000
    assert registry.get("knowledge_decision", "v2") is None


def test_renderer_rejects_for_loop_template(tmp_path) -> None:
    from app.core.prompts import SafePromptRenderer

    renderer = SafePromptRenderer()
    with pytest.raises(GatewayError) as exc_info:
        renderer.render("{% for x in items %}{{ x }}{% endfor %}", {})
    assert exc_info.value.code == "invalid_prompt_template"


def test_renderer_renders_if_branch() -> None:
    from app.core.prompts import SafePromptRenderer

    renderer = SafePromptRenderer()
    assert renderer.render("{% if flag %}是{% endif %}", {"flag": True}) == "是"
