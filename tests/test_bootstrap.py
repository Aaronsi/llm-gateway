"""启动级验证：仓库内 config.yaml 与 prompts 资产可直接加载、应用可组装。

配置资产不在 TDD 单测循环内（属配置类文件），此处用测试守护它们不漂移。
"""

from pathlib import Path

from app.core.config import AppConfig
from app.core.prompts import PromptRegistry

ROOT = Path(__file__).resolve().parents[1]


def test_config_yaml_loads_with_defaults() -> None:
    config = AppConfig.load(ROOT / "config.yaml")
    assert config.models["chat-fast"].priority == 100
    assert config.models["chat-pro"].structured_output_mode == "json_schema"
    assert config.retry.jitter is True
    assert config.admin_endpoints.enabled is False
    # API Key 只存环境变量名，不存密钥
    assert config.models["chat-fast"].api_key_env == "DEEPSEEK_API_KEY"


def test_prompt_assets_load() -> None:
    registry = PromptRegistry.load(ROOT / "prompts")
    package = registry.get("knowledge_decision", "v1")
    assert package is not None
    assert package.context_budget == 2000


def test_create_app_from_real_config() -> None:
    from app.app import create_app

    app = create_app(config=AppConfig.load(ROOT / "config.yaml"), prompts_dir=ROOT / "prompts")
    assert app.title == "llm-gateway"
