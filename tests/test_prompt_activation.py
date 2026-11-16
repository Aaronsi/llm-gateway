"""S11 Prompt 运行时激活：激活索引切换立即生效、显式 version 语义不变、激活端点独立认证。"""


import pytest

from app.core.config import AppConfig
from app.core.errors import GatewayError
from app.core.prompt_activation import InMemoryPromptActivationStore
from app.core.prompts import PromptEngine, PromptPackage, PromptRegistry, PromptVariable
from app.core.providers.base import ProviderResult
from app.core.schemas import Message, PromptSelection, Usage
from app.core.storage import SQLitePromptActivationStore


def _package(version: str) -> PromptPackage:
    return PromptPackage(
        name="greeter",
        version=version,
        system_template=f"{version}:你好{{{{ name }}}}",
        variables={"name": PromptVariable(max_length=20)},
    )


def _registry(**packages: PromptPackage) -> PromptRegistry:
    registry = PromptRegistry()
    for package in packages.values():
        registry.register(package)
    return registry


def _selection(version: str | None = None) -> PromptSelection:
    return PromptSelection(
        name="greeter",
        variables={"name": "小明"},
        **({"version": version} if version else {}),
    )


def test_registry_activate_switches_active_version() -> None:
    registry = _registry(v1=_package("v1"), v2=_package("v2"))
    assert registry.active_version("greeter") is None
    assert registry.activate("greeter", "v2") == "v2"
    assert registry.active_version("greeter") == "v2"
    registry.activate("greeter", "v1")
    assert registry.active_version("greeter") == "v1"


def test_activate_unknown_version_raises() -> None:
    registry = _registry(v1=_package("v1"))
    with pytest.raises(GatewayError) as exc_info:
        registry.activate("greeter", "v9")
    assert exc_info.value.code == "unknown_prompt_version"
    assert exc_info.value.status_code == 400
    assert registry.active_version("greeter") is None


def test_restore_active_skips_unknown_versions() -> None:
    registry = _registry(v1=_package("v1"))
    registry.restore_active({"greeter": "v9", "other": "v1"})
    assert registry.active_version("greeter") is None
    registry.restore_active({"greeter": "v1"})
    assert registry.active_version("greeter") == "v1"


def test_engine_resolves_active_version_when_not_specified() -> None:
    registry = _registry(v1=_package("v1"), v2=_package("v2"))
    registry.activate("greeter", "v2")
    engine = PromptEngine(registry)
    applied = engine.apply(_selection())
    assert applied.package.version == "v2"
    assert applied.rendered == "v2:你好小明"


def test_engine_explicit_version_unchanged() -> None:
    registry = _registry(v1=_package("v1"), v2=_package("v2"))
    registry.activate("greeter", "v2")
    engine = PromptEngine(registry)
    applied = engine.apply(_selection(version="v1"))
    assert applied.package.version == "v1"
    assert applied.rendered == "v1:你好小明"
    with pytest.raises(GatewayError) as exc_info:
        engine.apply(_selection(version="v9"))
    assert exc_info.value.code == "unknown_prompt_template"


def test_engine_requires_active_when_version_missing() -> None:
    registry = _registry(v1=_package("v1"))
    engine = PromptEngine(registry)
    with pytest.raises(GatewayError) as exc_info:
        engine.apply(_selection())
    assert exc_info.value.code == "unknown_prompt_template"


def test_inmemory_activation_store_round_trip() -> None:
    store = InMemoryPromptActivationStore()
    assert store.active_versions() == {}
    store.set_active("greeter", "v2")
    assert store.active_versions() == {"greeter": "v2"}


def test_sqlite_activation_store_round_trip(tmp_path) -> None:
    database_path = str(tmp_path / "gateway.db")
    store = SQLitePromptActivationStore(database_path)
    store.set_active("greeter", "v2")
    store.set_active("other", "v1")
    # 新实例从同一数据库恢复，验证跨重启存活。
    reopened = SQLitePromptActivationStore(database_path)
    assert reopened.active_versions() == {"greeter": "v2", "other": "v1"}
    reopened.set_active("greeter", "v1")
    assert reopened.active_versions()["greeter"] == "v1"


class RecordingProvider:
    capabilities = {"streaming", "structured_output"}

    def __init__(self) -> None:
        self.last_messages: list[Message] | None = None

    async def complete(self, config, call) -> ProviderResult:
        self.last_messages = list(call.messages)
        return ProviderResult(
            content="ok",
            usage=Usage(input_tokens=10, output_tokens=5),
            finish_reason="stop",
            provider_request_id="up-1",
        )

    async def stream(self, config, call):
        raise NotImplementedError

    async def close(self) -> None:
        pass


def _admin_config(admin_api_key=None) -> AppConfig:
    raw_admin = {"enabled": True}
    if admin_api_key is not None:
        raw_admin["admin_api_key"] = admin_api_key
    return AppConfig.model_validate(
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
                    "pricing": {"input": 1.0, "cached_input": 0.1, "output": 4.0},
                }
            },
            "admin_endpoints": raw_admin,
            "limits": {},
            "retry": {},
            "prompts": {},
            "logging": {},
        }
    )


@pytest.mark.asyncio
async def test_activate_endpoint_without_admin_key_returns_401(make_client) -> None:
    async with make_client(config=_admin_config()) as client:
        response = await client.post(
            "/v1/prompts/activate",
            json={"name": "greeter", "version": "v2"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_activate_endpoint_disabled_admin_router_404(make_client, sample_config) -> None:
    async with make_client(config=sample_config) as client:
        response = await client.post(
            "/v1/prompts/activate",
            json={"name": "greeter", "version": "v2"},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_activate_endpoint_rejects_wrong_admin_key(make_client) -> None:
    async with make_client(config=_admin_config("secret-admin")) as client:
        response = await client.post(
            "/v1/prompts/activate",
            json={"name": "greeter", "version": "v2"},
            headers={"Authorization": "Bearer wrong"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_activate_endpoint_switches_version_immediately(make_client, tmp_path) -> None:
    # 发布 v1/v2 资产；激活 v2 后，不带 version 的请求立即使用 v2。
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    for version, template in (("v1", "v1:你好{{ name }}"), ("v2", "v2:你好{{ name }}")):
        (prompts_dir / f"v{version}.yaml").write_text(
            f"""name: greeter
version: {version}
status: published
system_template: "{template}"
variables:
  name:
    type: str
    max_length: 20
""",
            encoding="utf-8",
        )
    provider = RecordingProvider()
    config = _admin_config("secret-admin")
    async with make_client(config=config, provider=provider, prompts_dir=prompts_dir) as client:
        response = await client.post(
            "/v1/prompts/activate",
            json={"name": "greeter", "version": "v2"},
            headers={"Authorization": "Bearer secret-admin"},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"name": "greeter", "version": "v2"}

        response = await client.post(
            "/v1/llm",
            json={
                "model": "chat-fast",
                "messages": [{"role": "user", "content": "hi"}],
                "prompt": {"name": "greeter", "variables": {"name": "小明"}},
            },
        )
        assert response.status_code == 200, response.text
    system_message = [m for m in provider.last_messages if m.role == "system"][0]
    assert system_message.content == "v2:你好小明"


@pytest.mark.asyncio
async def test_activate_unknown_version_returns_400(make_client, tmp_path) -> None:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "v1.yaml").write_text(
        """name: greeter
version: v1
status: published
system_template: "v1:你好{{ name }}"
variables:
  name:
    type: str
    max_length: 20
""",
        encoding="utf-8",
    )
    async with make_client(
        config=_admin_config("secret-admin"), prompts_dir=prompts_dir
    ) as client:
        response = await client.post(
            "/v1/prompts/activate",
            json={"name": "greeter", "version": "v9"},
            headers={"Authorization": "Bearer secret-admin"},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unknown_prompt_version"
