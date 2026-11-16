"""S1 认证：API Key 校验、401 语义、审计指纹记录与开放模式。

- 配置了 api_keys：无/错误 key → 401（invalid_api_key），Bearer 与 x-api-key 均可；
- 未配置 api_keys：开放模式（开发环境），审计记录匿名指纹；
- 审计白名单：CallTrace.api_key_hash 只存不可逆指纹。
"""

import pytest
from pydantic import SecretStr

from app.core.config import AppConfig
from app.core.security import key_fingerprint

MODELS = {
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
}


def make_auth_config(api_keys: list[str] | None, admin_enabled: bool = True) -> AppConfig:
    return AppConfig.model_validate(
        {
            "version": "1.0",
            "api_keys": [SecretStr(k) for k in (api_keys or [])],
            "models": MODELS,
            "admin_endpoints": {"enabled": admin_enabled},
        }
    )


def test_key_fingerprint_is_sha256_prefix() -> None:
    import hashlib

    assert key_fingerprint("secret-key") == hashlib.sha256(b"secret-key").hexdigest()[:16]
    assert key_fingerprint("") == "anonymous"
    assert key_fingerprint(None) == "anonymous"


@pytest.mark.asyncio
async def test_missing_key_returns_401(make_client) -> None:
    async with make_client(config=make_auth_config(["test-key"])) as client:
        response = await client.post(
            "/v1/llm", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_wrong_key_returns_401(make_client) -> None:
    async with make_client(config=make_auth_config(["test-key"])) as client:
        response = await client.post(
            "/v1/llm",
            headers={"Authorization": "Bearer wrong-key"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_bearer_key_authenticates_and_records_fingerprint(make_client) -> None:
    config = make_auth_config(["test-key"])
    async with make_client(config=config) as client:
        response = await client.post(
            "/v1/llm",
            headers={"Authorization": "Bearer test-key"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        traces = (await client.get("/v1/traces", headers={"Authorization": "Bearer test-key"})).json()
    assert len(traces) == 1
    assert traces[0]["api_key_hash"] == key_fingerprint("test-key")
    assert "test-key" not in traces[0]["api_key_hash"]


@pytest.mark.asyncio
async def test_x_api_key_header_authenticates(make_client) -> None:
    async with make_client(config=make_auth_config(["test-key"])) as client:
        response = await client.post(
            "/v1/llm",
            headers={"x-api-key": "test-key"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_admin_endpoints_require_auth(make_client) -> None:
    async with make_client(config=make_auth_config(["test-key"])) as client:
        response = await client.get("/v1/traces")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_open_mode_without_configured_keys(make_client) -> None:
    config = make_auth_config([])
    async with make_client(config=config) as client:
        response = await client.post(
            "/v1/llm", json={"messages": [{"role": "user", "content": "hi"}]}
        )
        assert response.status_code == 200
        traces = (await client.get("/v1/traces")).json()
    assert len(traces) == 1
    assert traces[0]["api_key_hash"] is not None
