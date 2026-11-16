"""Prompt 激活状态存储：管理端点切换的激活版本持久化，启动时恢复。

同步接口：写入只发生在管理动作（低频），恢复发生在应用装配期（create_app 同步上下文），
不在请求路径上，无需 to_thread。
"""

from typing import Protocol


class PromptActivationStore(Protocol):
    def set_active(self, name: str, version: str) -> None: ...

    def active_versions(self) -> dict[str, str]: ...


class InMemoryPromptActivationStore:
    def __init__(self) -> None:
        self._active: dict[str, str] = {}

    def set_active(self, name: str, version: str) -> None:
        self._active[name] = version

    def active_versions(self) -> dict[str, str]:
        return dict(self._active)
