"""YAML 加载、环境变量密钥名、能力子集校验、价格必填、默认值填充。"""

import pydantic
import pytest

from app.core.capabilities import Capability
from app.core.config import AppConfig


def _base_model(name: str = "m1", **overrides):
    data = {
        "adapter": "openai_compatible",
        "capabilities": ["streaming"],
        "priority": 50,
        "provider_model": "deepseek-x",
        "base_url": "https://api.example.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "pricing": {"input": 1.0, "cached_input": 0.1, "output": 4.0},
    }
    data.update(overrides)
    return {name: data}


def _config(models: dict) -> dict:
    return {
        "version": "1.0",
        "models": models,
        "limits": {},
        "retry": {},
        "prompts": {},
        "admin_endpoints": {},
        "logging": {},
    }


def test_load_yaml_file_and_defaults(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "version: '1.0'\n"
        "models:\n"
        "  chat-fast:\n"
        "    adapter: openai_compatible\n"
        "    capabilities: [streaming, structured_output]\n"
        "    priority: 100\n"
        "    provider_model: deepseek-v4-flash\n"
        "    base_url: https://api.deepseek.com\n"
        "    api_key_env: DEEPSEEK_API_KEY\n"
        "    structured_output_mode: json_object\n"
        "    pricing: {input: 1.0, cached_input: 0.1, output: 4.0}\n",
        encoding="utf-8",
    )
    config = AppConfig.load(path)
    assert config.version == "1.0"
    assert set(config.models) == {"chat-fast"}
    assert config.retry.max_attempts_per_model == 2
    assert config.admin_endpoints.enabled is False
    assert config.prompts.chars_per_token_estimate == 2.0


def test_capabilities_not_subset_of_adapter_raises() -> None:
    with pytest.raises(pydantic.ValidationError, match="tool_calling"):
        AppConfig.model_validate(_config(_base_model(capabilities=["tool_calling"])))


def test_unknown_adapter_raises() -> None:
    with pytest.raises(pydantic.ValidationError, match="adapter"):
        AppConfig.model_validate(_config(_base_model(adapter="anthropic")))


def test_missing_pricing_raises() -> None:
    model = _base_model()
    del model["m1"]["pricing"]
    with pytest.raises(pydantic.ValidationError):
        AppConfig.model_validate(_config(model))


def test_empty_api_key_env_raises() -> None:
    with pytest.raises(pydantic.ValidationError, match="api_key_env"):
        AppConfig.model_validate(_config(_base_model(api_key_env="")))


def test_resolve_model_returns_config_or_none() -> None:
    config = AppConfig.model_validate(_config(_base_model("chat-fast")))
    resolved = config.resolve_model("chat-fast")
    assert resolved is not None and resolved.provider_model == "deepseek-x"
    assert config.resolve_model("ghost") is None


def test_capabilities_parsed_as_enum_set() -> None:
    config = AppConfig.model_validate(_config(_base_model(capabilities=["streaming"])))
    assert config.models["m1"].capabilities == {Capability.STREAMING}


def test_retry_defaults_are_applied() -> None:
    config = AppConfig.model_validate(_config(_base_model()))
    assert config.retry.base_delay_seconds == 0.1
    assert config.retry.backoff_factor == 2.0
    assert config.retry.max_delay_seconds == 2.0
    assert config.retry.jitter is True


def test_missing_version_defaults_to_1_0() -> None:
    data = _config(_base_model())
    del data["version"]
    config = AppConfig.model_validate(data)
    assert config.version == "1.0"
