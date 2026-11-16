"""S7 端点补齐：/v1/models（OpenAI 形状）、healthz（免认证）、readyz（存储探活）。"""

import httpx
import pytest

from app.core.config import AppConfig

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
}


def make_config() -> AppConfig:
    return AppConfig.model_validate({"version": "1.0", "models": MODELS})


@pytest.mark.asyncio
async def test_healthz_is_open_without_auth(make_client) -> None:
    async with make_client(config=make_config()) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_reports_ready(make_client) -> None:
    async with make_client(config=make_config()) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_models_lists_configured_aliases_sorted(make_client) -> None:
    async with make_client(config=make_config()) as client:
        response = await client.get("/v1/models")
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    ids = [item["id"] for item in payload["data"]]
    assert ids == ["chat-fast", "chat-pro"]
    assert all(item["object"] == "model" for item in payload["data"])


@pytest.mark.asyncio
async def test_models_requires_auth_when_keys_configured(make_client) -> None:
    from pydantic import SecretStr

    config = AppConfig.model_validate(
        {"version": "1.0", "api_keys": [SecretStr("test-key")], "models": MODELS}
    )
    async with make_client(config=config) as client:
        unauthorized = await client.get("/v1/models")
        authorized = await client.get(
            "/v1/models", headers={"Authorization": "Bearer test-key"}
        )
    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


class UnhealthyStorage:
    async def ping(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_readyz_503_when_storage_unhealthy() -> None:
    from app.app import create_app

    class FakeProvider:
        capabilities = {"streaming", "structured_output"}

        async def complete(self, config, call):
            raise NotImplementedError

        async def stream(self, config, call):
            raise NotImplementedError

        async def close(self):
            pass

    app = create_app(
        config=make_config(), providers={"openai_compatible": FakeProvider()}
    )
    app.state.storage = UnhealthyStorage()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
