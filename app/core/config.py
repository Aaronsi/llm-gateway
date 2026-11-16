"""配置模型：YAML 基底加载、启动校验、模型白名单解析。

API Key 只以环境变量名形式出现在配置中，密钥本身由供应商适配器运行时读取。
网关调用方密钥在 YAML 中以 ${ENV_VAR} 引用声明，加载时展开为 SecretStr。
"""

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.core.capabilities import ADAPTER_CAPABILITIES, Capability
from app.core.rate_limit import RateLimitConfig

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env(value):
    """递归展开 ${ENV_VAR} 引用；未设置的变量展开为空字符串。"""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


class Pricing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: float = Field(ge=0)
    cached_input: float = Field(ge=0)
    output: float = Field(ge=0)


class ProviderConfig(BaseModel):
    # 供应商级配置：base_url/api_key_env 去重声明，timeout 统一管理。
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    timeout_seconds: float | None = Field(default=None, gt=0)


class ModelConfig(BaseModel):
    # 将平台模型名映射为供应商模型、地址、密钥环境变量与能力配置。
    # provider 引用 providers 段时内嵌 base_url/api_key_env 省略；未引用时必须内嵌。
    model_config = ConfigDict(extra="forbid")

    adapter: str
    capabilities: set[Capability]
    priority: int = Field(default=0, ge=0)
    weight: int = Field(default=1, ge=1)
    provider: str | None = None
    provider_model: str = Field(min_length=1)
    base_url: str | None = Field(default=None, min_length=1)
    api_key_env: str | None = Field(default=None, min_length=1)
    timeout_seconds: float | None = Field(default=None, gt=0)
    structured_output_mode: Literal["json_schema", "json_object"] = "json_schema"
    pricing: Pricing

    @model_validator(mode="after")
    def check_adapter_supports_capabilities(self) -> "ModelConfig":
        adapter_caps = ADAPTER_CAPABILITIES.get(self.adapter)
        if adapter_caps is None:
            raise ValueError(f"unknown adapter: {self.adapter}")
        extra = self.capabilities - adapter_caps
        if extra:
            raise ValueError(
                f"capabilities not supported by adapter {self.adapter}: {sorted(extra)}"
            )
        return self

    @model_validator(mode="after")
    def check_connection_fields(self) -> "ModelConfig":
        if self.provider is None:
            if self.base_url is None or self.api_key_env is None:
                raise ValueError("未引用 provider 时必须内嵌 base_url 与 api_key_env")
        elif self.base_url is not None or self.api_key_env is not None:
            raise ValueError(
                "引用 provider 时 base_url/api_key_env 由 providers 段解析，不可内嵌"
            )
        return self


class HealthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consecutive_failure_threshold: int = Field(default=3, ge=1)
    cooldown_seconds: float = Field(default=30, gt=0)


class WeightedRoundRobinConfig(BaseModel):
    # 同 priority、cost 在容差内的候选组内按 weight 加权轮换首选项。
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    cost_tolerance_ratio: float = Field(default=0.1, ge=0)


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    health: HealthConfig = Field(default_factory=HealthConfig)
    weighted_round_robin: WeightedRoundRobinConfig = Field(
        default_factory=WeightedRoundRobinConfig
    )


class StreamCheckpointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    flush_interval_seconds: float = Field(default=2.0, ge=0)
    max_chars: int = Field(default=10_000, ge=1)


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrent_requests: int = Field(default=100, ge=1)
    per_model_max_concurrent: int = Field(default=20, ge=1)
    queue_timeout_seconds: float = Field(default=5, gt=0)
    min_remaining_budget_seconds: float = Field(default=1.0, ge=0)
    rate: RateLimitConfig = Field(default_factory=RateLimitConfig)
    stream_checkpoint: StreamCheckpointConfig = Field(default_factory=StreamCheckpointConfig)


class RetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts_per_model: int = Field(default=2, ge=1)
    base_delay_seconds: float = Field(default=0.1, ge=0)
    backoff_factor: float = Field(default=2.0, ge=1)
    max_delay_seconds: float = Field(default=2.0, ge=0)
    jitter: bool = True


class RepairConfig(BaseModel):
    # LLM feedback 修复：失败输出 + 校验错误作为新消息追加，重生成次数有界。
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_llm_repair_attempts: int = Field(default=1, ge=0)


class StructuredOutputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repair: RepairConfig = Field(default_factory=RepairConfig)


class PromptsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chars_per_token_estimate: float = Field(default=2.0, gt=0)


class AdminConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    admin_api_key: SecretStr | None = None


class ApiConfig(BaseModel):
    # 错误响应形状：gateway 为 {"detail": ...}，openai 为 {"error": ...}（SDK 兼容）。
    model_config = ConfigDict(extra="forbid")

    error_shape: Literal["gateway", "openai"] = "gateway"


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["memory", "sqlite"] = "memory"
    database_path: str = "data/gateway.db"
    metrics_flush_interval_seconds: float = Field(default=10, gt=0)


class HandlerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handler: str = "console"
    format: str = "json"


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str = "INFO"
    access: HandlerConfig = Field(default_factory=HandlerConfig)
    error: HandlerConfig = Field(default_factory=HandlerConfig)
    audit: HandlerConfig = Field(default_factory=HandlerConfig)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = "1.0"
    api_keys: list[SecretStr] = Field(default_factory=list)
    models: dict[str, ModelConfig]
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    structured_output: StructuredOutputConfig = Field(default_factory=StructuredOutputConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    admin_endpoints: AdminConfig = Field(default_factory=AdminConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    def resolve_model(self, name: str) -> ModelConfig | None:
        return self.models.get(name)

    @model_validator(mode="after")
    def resolve_provider_references(self) -> "AppConfig":
        # 启动校验：引用未知 provider 直接报错；命中则把供应商字段解析进模型配置。
        for name, model in self.models.items():
            if model.provider is None:
                continue
            provider = self.providers.get(model.provider)
            if provider is None:
                raise ValueError(f"model {name} 引用了未知 provider: {model.provider}")
            model.base_url = provider.base_url
            model.api_key_env = provider.api_key_env
            model.timeout_seconds = provider.timeout_seconds
        return self

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(_expand_env(data))
