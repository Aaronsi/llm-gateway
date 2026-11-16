"""S2 速率限流：进程内令牌桶、429 语义、Retry-After 与时钟注入。"""

import pytest

from app.core.errors import GatewayError
from app.core.rate_limit import InMemoryRateLimiter, RateLimitConfig


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_burst_exceeded_raises_429() -> None:
    limiter = InMemoryRateLimiter(
        RateLimitConfig(enabled=True, requests_per_minute=60, burst=3),
        clock=FakeClock(),
    )
    for _ in range(3):
        await limiter.check("caller-a")
    with pytest.raises(GatewayError) as exc_info:
        await limiter.check("caller-a")
    assert exc_info.value.code == "rate_limit_exceeded"
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after >= 1


@pytest.mark.asyncio
async def test_disabled_limiter_never_blocks() -> None:
    limiter = InMemoryRateLimiter(
        RateLimitConfig(enabled=False, requests_per_minute=60, burst=1),
        clock=FakeClock(),
    )
    for _ in range(100):
        await limiter.check("caller-a")


@pytest.mark.asyncio
async def test_tokens_refill_over_time() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(
        RateLimitConfig(enabled=True, requests_per_minute=60, burst=2),
        clock=clock,
    )
    await limiter.check("caller-a")
    await limiter.check("caller-a")
    with pytest.raises(GatewayError):
        await limiter.check("caller-a")
    clock.now += 60.0  # 60 rpm → 1 秒 1 token，60 秒后满额
    await limiter.check("caller-a")
    await limiter.check("caller-a")


@pytest.mark.asyncio
async def test_per_identity_buckets_are_independent() -> None:
    limiter = InMemoryRateLimiter(
        RateLimitConfig(enabled=True, requests_per_minute=60, burst=1),
        clock=FakeClock(),
    )
    await limiter.check("caller-a")
    await limiter.check("caller-b")  # 不同身份的桶互不影响
    with pytest.raises(GatewayError):
        await limiter.check("caller-a")


@pytest.mark.asyncio
async def test_api_rate_limit_returns_429_with_retry_after(make_client) -> None:
    from app.core.config import AppConfig

    config = AppConfig.model_validate(
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
                }
            },
            "limits": {"rate": {"enabled": True, "requests_per_minute": 60, "burst": 2}},
        }
    )
    async with make_client(config=config) as client:
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        first = await client.post("/v1/llm", json=payload)
        second = await client.post("/v1/llm", json=payload)
        third = await client.post("/v1/llm", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.json()["detail"]["code"] == "rate_limit_exceeded"
    assert int(third.headers["retry-after"]) >= 1
