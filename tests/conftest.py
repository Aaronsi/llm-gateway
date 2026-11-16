import pytest

from app.core.config import AppConfig


@pytest.fixture
def sample_config() -> AppConfig:
    """两个模型的最小完整配置，测试默认值填充与路由输入。"""
    return AppConfig.model_validate(
        {
            "version": "1.0",
            "models": {
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
                "chat-pro": {
                    "adapter": "openai_compatible",
                    "capabilities": ["streaming", "structured_output"],
                    "priority": 80,
                    "provider_model": "deepseek-v4-pro",
                    "base_url": "https://api.deepseek.com",
                    "api_key_env": "DEEPSEEK_BACKUP_API_KEY",
                    "structured_output_mode": "json_schema",
                    "pricing": {"input": 0.8, "cached_input": 0.08, "output": 3.2},
                },
            },
            "routing": {"health": {"consecutive_failure_threshold": 3, "cooldown_seconds": 30}},
            "limits": {},
            "retry": {},
            "prompts": {},
            "admin_endpoints": {},
            "logging": {},
        }
    )


@pytest.fixture
def health_tracker():
    from app.core.health import HealthTracker

    return HealthTracker(consecutive_failure_threshold=3, cooldown_seconds=30, clock=lambda: 0.0)


@pytest.fixture
def fake_sleep():
    """记录睡眠时长、零真实等待的 sleep 替身。"""

    def _fake_sleep(seconds: float):
        calls.append(seconds)

    calls: list[float] = []
    _fake_sleep.calls = calls  # type: ignore[attr-defined]
    return _fake_sleep


class ApiFakeProvider:
    """API 层测试默认供应商：成功返回 ok，可配置失败次数与输出内容。"""
    capabilities = {"streaming", "structured_output"}

    def __init__(self, content: str = "ok", failures: int = 0) -> None:
        self.content = content
        self.failures = failures

    async def complete(self, config, call):
        from app.core.providers.base import ProviderResult, ProviderTimeoutError
        from app.core.schemas import Usage

        if self.failures > 0:
            self.failures -= 1
            raise ProviderTimeoutError()
        return ProviderResult(
            content=self.content,
            usage=Usage(input_tokens=10, output_tokens=5),
            finish_reason="stop",
            provider_request_id="up-1",
        )

    async def stream(self, config, call):
        from app.core.providers.base import StreamChunk

        yield StreamChunk(text="你", usage=None)
        yield StreamChunk(text="好", usage=None)

    async def close(self) -> None:
        pass


@pytest.fixture
def make_client(sample_config):
    """构造 create_app + ASGI client 的工厂；可注入 config/providers/prompts_dir。"""
    from contextlib import asynccontextmanager

    import httpx

    from app.app import create_app

    @asynccontextmanager
    async def _factory(config=None, providers=None, prompts_dir=None, provider=None):
        if provider is not None:
            providers = {"openai_compatible": provider}
        app = create_app(
            config=config or sample_config,
            providers=providers if providers is not None else {"openai_compatible": ApiFakeProvider()},
            prompts_dir=prompts_dir,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    return _factory
