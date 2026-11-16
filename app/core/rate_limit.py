"""进程内令牌桶限流（每身份独立桶）。

多副本部署时接口边界保持不变，替换为 Redis 等共享存储实现即可。
"""

import asyncio
import time
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import GatewayError


class RateLimitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    requests_per_minute: int = Field(default=60, ge=1)
    burst: int = Field(default=10, ge=1)


@dataclass
class Bucket:
    tokens: float
    updated_at: float


class InMemoryRateLimiter:
    """Process-local token bucket. Use Redis when running multiple replicas."""

    def __init__(self, config: RateLimitConfig, clock=time.monotonic) -> None:
        self.config = config
        self._clock = clock
        self._buckets: dict[str, Bucket] = {}
        self._lock = asyncio.Lock()

    async def check(self, identity: str) -> None:
        if not self.config.enabled:
            return
        now = self._clock()
        refill_per_second = self.config.requests_per_minute / 60
        capacity = float(self.config.burst)
        async with self._lock:
            bucket = self._buckets.setdefault(identity, Bucket(capacity, now))
            bucket.tokens = min(capacity, bucket.tokens + (now - bucket.updated_at) * refill_per_second)
            bucket.updated_at = now
            if bucket.tokens < 1:
                retry_after = max(1, int((1 - bucket.tokens) / refill_per_second) + 1)
                raise GatewayError(
                    "rate_limit_exceeded",
                    f"请求过于频繁，请在 {retry_after}s 后重试",
                    429,
                    retry_after=retry_after,
                )
            bucket.tokens -= 1
