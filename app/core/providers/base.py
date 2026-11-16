"""供应商适配器协议、调用/结果数据类与分层异常层级。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from app.core.capabilities import Capability
from app.core.config import ModelConfig
from app.core.errors import GatewayError
from app.core.schemas import Message, Usage


class ProviderError(GatewayError):
    # 分层异常：retryable / fallback_allowed 属性驱动服务层重试与降级决策。
    retryable: ClassVar[bool] = False
    fallback_allowed: ClassVar[bool] = False


class ProviderConnectionError(ProviderError):
    retryable = True
    fallback_allowed = True

    def __init__(
        self,
        code: str = "provider_connection_error",
        message: str = "上游连接失败",
        status_code: int = 502,
    ) -> None:
        super().__init__(code, message, status_code)


class ProviderTimeoutError(ProviderError):
    retryable = True
    fallback_allowed = True

    def __init__(
        self,
        code: str = "provider_timeout",
        message: str = "上游请求超时",
        status_code: int = 502,
    ) -> None:
        super().__init__(code, message, status_code)


class ProviderRateLimitError(ProviderError):
    retryable = True
    fallback_allowed = True

    def __init__(
        self,
        code: str = "provider_rate_limited",
        message: str = "上游限流",
        retry_after_seconds: float | None = None,
        status_code: int = 429,
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code, message, status_code)


class ProviderServerError(ProviderError):
    retryable = True
    fallback_allowed = True

    def __init__(
        self,
        code: str = "provider_server_error",
        message: str = "上游服务器错误",
        status_code: int = 502,
    ) -> None:
        super().__init__(code, message, status_code)


class ProviderAuthError(ProviderError):
    def __init__(
        self,
        code: str = "provider_auth_error",
        message: str = "上游认证失败",
        status_code: int = 503,
    ) -> None:
        super().__init__(code, message, status_code)


class ProviderBadRequestError(ProviderError):
    def __init__(
        self,
        code: str = "provider_bad_request",
        message: str = "上游拒绝请求",
        status_code: int = 502,
    ) -> None:
        super().__init__(code, message, status_code)


class ProviderInvalidResponseError(ProviderError):
    # L3 协议错误：可降级换模型，但不重试（同请求重试大概率复现）。
    fallback_allowed = True

    def __init__(
        self,
        code: str = "provider_invalid_response",
        message: str = "上游响应不符合协议",
        status_code: int = 502,
    ) -> None:
        super().__init__(code, message, status_code)


@dataclass(frozen=True)
class ProviderCall:
    messages: list[Message]
    timeout_seconds: float
    response_schema: dict[str, Any] | None


@dataclass(frozen=True)
class ProviderResult:
    content: str
    usage: Usage
    finish_reason: str | None
    provider_request_id: str | None


@dataclass(frozen=True)
class StreamChunk:
    """流式增量：text 为内容增量；usage 仅末块携带（上游支持真实统计时）。"""

    text: str | None
    usage: Usage | None


class Provider(Protocol):
    capabilities: ClassVar[set[Capability]]

    async def complete(
        self, config: ModelConfig, call: ProviderCall
    ) -> ProviderResult: ...

    async def stream(
        self, config: ModelConfig, call: ProviderCall
    ) -> AsyncIterator[StreamChunk]: ...

    async def close(self) -> None: ...
