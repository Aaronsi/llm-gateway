"""S12 配置 providers 独立段：base_url/api_key_env 去重引用、未知 provider 启动报错、供应商超时上限。"""

import asyncio

import pytest
from pydantic import ValidationError

from app.core.audit import InMemoryAuditStore, RoutingDecisionLog
from app.core.config import AppConfig
from app.core.health import HealthTracker
from app.core.limits import CapacityGuard
from app.core.metrics import InMemoryMetricsRecorder
from app.core.prompts import PromptEngine, PromptRegistry
from app.core.providers.base import ProviderResult
from app.core.router import ModelRouter
from app.core.schemas import LLMRequest, Message, Usage
from app.service.llm_service import LLMService

MODEL_BASE = {
    "adapter": "openai_compatible",
    "capabilities": ["streaming", "structured_output"],
    "priority": 100,
    "provider_model": "deepseek-v4-flash",
    "structured_output_mode": "json_object",
    "pricing": {"input": 1.0, "cached_input": 0.1, "output": 4.0},
}


def _config(**overrides) -> AppConfig:
    raw = {"version": "1.0", "models": {"chat-fast": dict(MODEL_BASE)}}
    raw.update(overrides)
    return AppConfig.model_validate(raw)


def test_embedded_fields_unchanged_without_providers_section() -> None:
    # 回归：无 providers 段时内嵌 base_url/api_key_env 行为不变。
    config = _config(
        models={
            "chat-fast": {
                **MODEL_BASE,
                "base_url": "https://api.deepseek.com",
                "api_key_env": "DEEPSEEK_API_KEY",
            }
        }
    )
    model = config.resolve_model("chat-fast")
    assert model.base_url == "https://api.deepseek.com"
    assert model.api_key_env == "DEEPSEEK_API_KEY"
    assert model.timeout_seconds is None


def test_model_without_provider_ref_requires_embedded_fields() -> None:
    with pytest.raises(ValidationError):
        _config()


def test_provider_ref_resolves_base_url_and_key_env() -> None:
    config = _config(
        providers={
            "deepseek": {
                "base_url": "https://api.deepseek.com",
                "api_key_env": "DEEPSEEK_API_KEY",
                "timeout_seconds": 30.0,
            }
        },
        models={"chat-fast": {**MODEL_BASE, "provider": "deepseek"}},
    )
    model = config.resolve_model("chat-fast")
    assert model.base_url == "https://api.deepseek.com"
    assert model.api_key_env == "DEEPSEEK_API_KEY"
    assert model.timeout_seconds == 30.0


def test_provider_ref_with_embedded_fields_rejected() -> None:
    # 引用 provider 时内嵌字段必须省略，避免两处声明的漂移歧义。
    with pytest.raises(ValidationError):
        _config(
            providers={"deepseek": {"base_url": "https://x", "api_key_env": "K"}},
            models={
                "chat-fast": {
                    **MODEL_BASE,
                    "provider": "deepseek",
                    "base_url": "https://y",
                    "api_key_env": "K2",
                }
            },
        )


def test_unknown_provider_ref_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _config(models={"chat-fast": {**MODEL_BASE, "provider": "ghost"}})
    assert "ghost" in str(exc_info.value)


class RecordingProvider:
    capabilities = {"streaming", "structured_output"}

    def __init__(self) -> None:
        self.last_timeout: float | None = None

    async def complete(self, config, call) -> ProviderResult:
        self.last_timeout = call.timeout_seconds
        return ProviderResult(
            content="ok",
            usage=Usage(input_tokens=10, output_tokens=5),
            finish_reason="stop",
            provider_request_id="up-1",
        )

    async def stream(self, config, call):
        raise NotImplementedError

    async def close(self) -> None:
        pass


def _service(config, provider):
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
        clock=lambda: 0.0,
        providers={"openai_compatible": provider},
    )


def _request(timeout_seconds: float = 30) -> LLMRequest:
    return LLMRequest(
        messages=[Message(role="user", content="hi")],
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.asyncio
async def test_provider_timeout_caps_attempt_timeout() -> None:
    # 供应商 timeout_seconds 统一管理：上游调用超时取 min(请求预算, 供应商上限)。
    config = _config(
        providers={
            "deepseek": {
                "base_url": "https://api.deepseek.com",
                "api_key_env": "DEEPSEEK_API_KEY",
                "timeout_seconds": 1.0,
            }
        },
        models={"chat-fast": {**MODEL_BASE, "provider": "deepseek"}},
    )
    provider = RecordingProvider()
    service = _service(config, provider)
    await service.complete(_request(timeout_seconds=30))
    assert provider.last_timeout == 1.0


@pytest.mark.asyncio
async def test_uncapped_timeout_when_provider_has_no_limit() -> None:
    config = _config(
        providers={"deepseek": {"base_url": "https://api.deepseek.com", "api_key_env": "K"}},
        models={"chat-fast": {**MODEL_BASE, "provider": "deepseek"}},
    )
    provider = RecordingProvider()
    service = _service(config, provider)
    await service.complete(_request(timeout_seconds=30))
    assert provider.last_timeout == 30.0
