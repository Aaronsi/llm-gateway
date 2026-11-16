"""将内部错误标准化为可安全暴露给调用方的稳定错误码和 HTTP 状态。"""


class GatewayError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 502,
        retry_after: float | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(message)
