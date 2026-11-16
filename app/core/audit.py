"""审计存储与路由决策留痕（内存实现，接口可替换为持久化存储）。

接口为异步：SQLite 等持久化实现经 to_thread 执行 I/O；调用方 await 不阻塞事件循环。
"""

from typing import Protocol

from app.core.schemas import CallTrace, RoutingDecision


class AuditStore(Protocol):
    async def append(self, trace: CallTrace) -> None: ...

    async def list(self, limit: int = 100) -> list[CallTrace]: ...


class InMemoryAuditStore:
    def __init__(self) -> None:
        self._traces: list[CallTrace] = []

    async def append(self, trace: CallTrace) -> None:
        self._traces.append(trace)

    async def list(self, limit: int = 100) -> list[CallTrace]:
        return list(self._traces[-limit:])

    async def ping(self) -> bool:
        return True


class DecisionLog(Protocol):
    async def append(self, decision: RoutingDecision) -> None: ...

    async def list(self, limit: int = 100) -> list[RoutingDecision]: ...


class RoutingDecisionLog:
    def __init__(self) -> None:
        self._decisions: list[RoutingDecision] = []

    async def append(self, decision: RoutingDecision) -> None:
        self._decisions.append(decision)

    async def list(self, limit: int = 100) -> list[RoutingDecision]:
        return list(self._decisions[-limit:])
