"""认证：Bearer/x-api-key 校验、不可逆指纹与身份 ContextVar。

- 未配置 api_keys：开放模式（开发环境），身份为客户端地址指纹；
- 配置了 api_keys：常量时间比较（hmac.compare_digest），失败 → invalid_api_key(401)；
- 只对外暴露指纹（sha256 前 16 位），审计不落原始 key。
"""

import hashlib
import hmac
from contextvars import ContextVar

from fastapi import Header, Request

from app.core.errors import GatewayError

# 认证成功后写入的身份指纹；service 层据此填充 CallTrace.api_key_hash。
identity_var: ContextVar[str | None] = ContextVar("identity", default=None)


def key_fingerprint(value: str | None) -> str:
    if not value:
        return "anonymous"
    return hashlib.sha256(value.encode()).hexdigest()[:16]


async def authenticate(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> str:
    configured = [
        item.get_secret_value()
        for item in request.app.state.config.api_keys
        if item.get_secret_value()
    ]
    if not configured:
        fingerprint = key_fingerprint(request.client.host if request.client else None)
        identity_var.set(fingerprint)
        return fingerprint

    supplied = x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not any(hmac.compare_digest(supplied, candidate) for candidate in configured):
        raise GatewayError("invalid_api_key", "无效或缺失的 API Key", 401)
    fingerprint = key_fingerprint(supplied)
    identity_var.set(fingerprint)
    return fingerprint


async def authenticate_admin(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    # 管理端点独立认证：未配置 admin_api_key 时激活端点不可用（401）。
    admin_key = request.app.state.config.admin_endpoints.admin_api_key
    if admin_key is None:
        raise GatewayError("admin_auth_not_configured", "管理端点未配置独立认证", 401)
    supplied = x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not hmac.compare_digest(supplied, admin_key.get_secret_value()):
        raise GatewayError("invalid_api_key", "无效或缺失的管理 API Key", 401)
