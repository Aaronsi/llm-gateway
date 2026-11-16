"""流检查点存储：断线找回的文本增量持久化接口（内存/SQLite 双实现）。

内容只存已解析文本增量与终态状态，不含请求/回答原文之外的信息；
写失败由服务层吞异常，绝不阻断流式响应。
"""

from datetime import UTC, datetime
from typing import Any, Protocol


class CheckpointStore(Protocol):
    async def save(
        self, request_id: str, api: str, model: str, status: str, content: str
    ) -> None: ...

    async def get(self, request_id: str) -> dict[str, Any] | None: ...


class InMemoryCheckpointStore:
    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}

    async def save(
        self, request_id: str, api: str, model: str, status: str, content: str
    ) -> None:
        self._rows[request_id] = {
            "request_id": request_id,
            "api": api,
            "model": model,
            "status": status,
            "content": content,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    async def get(self, request_id: str) -> dict[str, Any] | None:
        return self._rows.get(request_id)
