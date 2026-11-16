"""供应商适配器注册表：register_provider/get_provider。

get_provider 按 adapter 名单例缓存实例，保证客户端连接池跨请求复用；
service 只经 get_provider(model.adapter) 拿协议对象，对供应商零感知。
"""

from app.core.errors import GatewayError
from app.core.providers.base import Provider

_PROVIDER_REGISTRY: dict[str, type[Provider]] = {}
_PROVIDER_INSTANCES: dict[str, Provider] = {}


def register_provider(name: str, cls: type[Provider]) -> None:
    _PROVIDER_REGISTRY[name] = cls


def get_provider(name: str) -> Provider:
    cls = _PROVIDER_REGISTRY.get(name)
    if cls is None:
        raise GatewayError("unknown_provider_adapter", f"未知供应商适配器: {name}", 503)
    if name not in _PROVIDER_INSTANCES:
        _PROVIDER_INSTANCES[name] = cls()
    return _PROVIDER_INSTANCES[name]


async def close_all_providers() -> None:
    """关闭全部已实例化适配器的连接池（应用退出时调用）。"""
    providers = list(_PROVIDER_INSTANCES.values())
    _PROVIDER_INSTANCES.clear()
    for provider in providers:
        try:
            await provider.close()
        except Exception:  # noqa: BLE001 —— 关闭失败不阻断退出
            pass


from app.core.providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
)

register_provider("openai_compatible", OpenAICompatibleProvider)
