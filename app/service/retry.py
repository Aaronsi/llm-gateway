"""重试策略纯函数：退避计算与可重试/可降级判定。"""

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts_per_model: int = 2
    base_delay_seconds: float = 0.1
    backoff_factor: float = 2.0
    max_delay_seconds: float = 2.0
    jitter: bool = True


def is_retryable_error(exc: Exception) -> bool:
    # 查 ProviderError.retryable 属性；其余异常默认不可重试。
    return bool(getattr(exc, "retryable", False))


def allow_fallback(exc: Exception) -> bool:
    # 查 ProviderError.fallback_allowed 属性；其余异常默认不降级。
    return bool(getattr(exc, "fallback_allowed", False))


def backoff_delay(attempt: int, policy: RetryPolicy) -> float:
    # delay = min(base * factor^(attempt-1), max)；jitter=true 时乘以 [0.5, 1.5] 随机系数。
    delay = min(
        policy.base_delay_seconds * (policy.backoff_factor ** (attempt - 1)),
        policy.max_delay_seconds,
    )
    if policy.jitter:
        delay *= random.uniform(0.5, 1.5)
    return delay
