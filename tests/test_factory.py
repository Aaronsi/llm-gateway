"""进程入口级验证：uvicorn --factory 零参启动路径（S13 工程化）。

create_app() 不带 config 参数时为进程引导模式：从 GATEWAY_CONFIG 环境变量
（缺省 config.yaml）加载配置，并自动加载当前目录 prompts/（存在时）。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_create_app_loads_config_from_gateway_config_env(monkeypatch):
    from app.app import create_app

    monkeypatch.setenv("GATEWAY_CONFIG", str(ROOT / "config.yaml"))
    monkeypatch.chdir(ROOT)
    app = create_app()
    assert app.title == "llm-gateway"
    assert app.state.config.models["chat-fast"].priority == 100


def test_create_app_falls_back_to_default_config_yaml(monkeypatch):
    from app.app import create_app

    monkeypatch.delenv("GATEWAY_CONFIG", raising=False)
    monkeypatch.chdir(ROOT)
    app = create_app()
    assert app.title == "llm-gateway"
    assert app.state.config.models["chat-fast"].priority == 100


def test_create_app_env_boot_autoloads_prompts_dir(monkeypatch):
    from app.app import create_app

    monkeypatch.setenv("GATEWAY_CONFIG", str(ROOT / "config.yaml"))
    monkeypatch.chdir(ROOT)
    app = create_app()
    assert app.state.prompt_registry.get("knowledge_decision", "v1") is not None
