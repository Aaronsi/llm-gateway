"""GatewayError → HTTP 错误响应统一映射；api.error_shape 切换 gateway/openai 形状。"""

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.errors import GatewayError


def to_json_response(exc: GatewayError, error_shape: str = "gateway") -> JSONResponse:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
    if error_shape == "openai":
        # OpenAI 形状：稳定 code 映射到 type；param/code 无对应信息置空。
        content = {"error": {"message": exc.message, "type": exc.code, "param": None, "code": None}}
    else:
        content = {"detail": {"code": exc.code, "message": exc.message}}
    return JSONResponse(status_code=exc.status_code, content=content, headers=headers)


async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
    config = getattr(request.app.state, "config", None)
    error_shape = getattr(getattr(config, "api", None), "error_shape", "gateway")
    return to_json_response(exc, error_shape)
