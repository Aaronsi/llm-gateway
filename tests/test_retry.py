"""③ 重试策略纯函数测试：退避序列、封顶、jitter 区间、可重试/可降级判定。"""

import pytest

from app.core.providers.base import (
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderConnectionError,
    ProviderInvalidResponseError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
)
from app.service.retry import (
    RetryPolicy,
    allow_fallback,
    backoff_delay,
    is_retryable_error,
)


@pytest.fixture
def policy() -> RetryPolicy:
    return RetryPolicy(
        max_attempts_per_model=2,
        base_delay_seconds=0.1,
        backoff_factor=2.0,
        max_delay_seconds=2.0,
        jitter=False,
    )


def test_backoff_delay_first_attempt_is_base(policy: RetryPolicy) -> None:
    assert backoff_delay(1, policy) == 0.1


def test_backoff_delay_grows_exponentially(policy: RetryPolicy) -> None:
    assert backoff_delay(2, policy) == 0.2
    assert backoff_delay(3, policy) == 0.4


def test_backoff_delay_is_capped_at_max(policy: RetryPolicy) -> None:
    assert backoff_delay(10, policy) == 2.0


def test_backoff_delay_jitter_stays_within_half_to_double() -> None:
    jittery = RetryPolicy(base_delay_seconds=0.1, jitter=True)
    for _ in range(100):
        delay = backoff_delay(1, jittery)
        assert 0.05 <= delay <= 0.15


def test_connection_error_is_retryable() -> None:
    assert is_retryable_error(ProviderConnectionError("provider_connection_error", "x"))


def test_timeout_and_rate_limit_and_server_error_are_retryable() -> None:
    assert is_retryable_error(ProviderTimeoutError("provider_timeout", "x"))
    assert is_retryable_error(ProviderRateLimitError("provider_rate_limited", "x"))
    assert is_retryable_error(ProviderServerError("provider_server_error", "x"))


def test_client_and_auth_errors_are_not_retryable() -> None:
    assert not is_retryable_error(ProviderBadRequestError("provider_bad_request", "x"))
    assert not is_retryable_error(ProviderAuthError("provider_auth_error", "x"))


def test_plain_exception_is_not_retryable() -> None:
    assert not is_retryable_error(ValueError("boom"))


def test_connection_error_allows_fallback() -> None:
    assert allow_fallback(ProviderTimeoutError("provider_timeout", "x"))


def test_invalid_response_allows_fallback_but_not_retry() -> None:
    exc = ProviderInvalidResponseError("provider_invalid_response", "x")
    assert allow_fallback(exc)
    assert not is_retryable_error(exc)


def test_auth_error_does_not_allow_fallback() -> None:
    assert not allow_fallback(ProviderAuthError("provider_auth_error", "x"))
