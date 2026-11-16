"""SQLite 持久化实现：审计、路由决策留痕、指标快照与流检查点。

- 白名单原则：只落 CallTrace / RoutingDecision 已声明字段，不保存 Prompt 正文与回答内容；
- 所有 I/O 经 asyncio.to_thread 执行；写失败吞异常并记日志，绝不阻断请求路径；
- 懒初始化（首次访问建表），不依赖 lifespan；跨重启数据存活。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.metrics import InMemoryMetricsRecorder
from app.core.schemas import CallTrace, RoutingDecision

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    call_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    api_key_hash TEXT,
    requested_model TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at);
CREATE TABLE IF NOT EXISTS routing_decisions (
    call_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_checkpoints (
    request_id TEXT PRIMARY KEY,
    api TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _connect(database_path: str) -> sqlite3.Connection:
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    return connection


class _SqliteBase:
    """懒初始化 + to_thread I/O 的公共基类。"""

    def __init__(self, database_path: str) -> None:
        self._database_path = database_path
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._create_schema, self._database_path)
            self._initialized = True

    @staticmethod
    def _create_schema(database_path: str) -> None:
        with _connect(database_path) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    async def ping(self) -> bool:
        try:
            await self.initialize()
            return True
        except Exception:  # noqa: BLE001 —— 建表/建库失败即视为不可用
            return False


class SQLiteAuditStore(_SqliteBase):
    """CallTrace 白名单落盘；append 失败只记日志。"""

    async def append(self, trace: CallTrace) -> None:
        try:
            await self.initialize()
            payload = trace.model_dump_json()
            await asyncio.to_thread(self._insert, self._database_path, trace, payload)
        except Exception:  # noqa: BLE001 —— 审计写失败不阻断请求
            logger.exception("Failed to persist trace %s", trace.call_id)

    @staticmethod
    def _insert(database_path: str, trace: CallTrace, payload: str) -> None:
        with _connect(database_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO traces VALUES (?,?,?,?,?,?)",
                (
                    trace.call_id,
                    _now(),
                    trace.status,
                    trace.api_key_hash,
                    trace.requested_model,
                    payload,
                ),
            )
            conn.commit()

    async def list(self, limit: int = 100) -> list[CallTrace]:
        await self.initialize()
        rows = await asyncio.to_thread(self._select, self._database_path, limit)
        return [CallTrace.model_validate(json.loads(payload)) for payload in rows]

    @staticmethod
    def _select(database_path: str, limit: int) -> list[str]:
        with _connect(database_path) as conn:
            cursor = conn.execute(
                "SELECT payload_json FROM traces ORDER BY created_at DESC LIMIT ?",
                (min(limit, 1000),),
            )
            return [row[0] for row in cursor.fetchall()]


class SQLiteRoutingDecisionLog(_SqliteBase):
    async def append(self, decision: RoutingDecision) -> None:
        try:
            await self.initialize()
            payload = decision.model_dump_json()
            await asyncio.to_thread(self._insert, self._database_path, decision, payload)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist routing decision %s", decision.call_id)

    @staticmethod
    def _insert(database_path: str, decision: RoutingDecision, payload: str) -> None:
        with _connect(database_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO routing_decisions VALUES (?,?,?)",
                (decision.call_id, _now(), payload),
            )
            conn.commit()

    async def list(self, limit: int = 100) -> list[RoutingDecision]:
        await self.initialize()
        rows = await asyncio.to_thread(self._select, self._database_path, limit)
        return [RoutingDecision.model_validate(json.loads(payload)) for payload in rows]

    @staticmethod
    def _select(database_path: str, limit: int) -> list[str]:
        with _connect(database_path) as conn:
            cursor = conn.execute(
                "SELECT payload_json FROM routing_decisions ORDER BY created_at DESC LIMIT ?",
                (min(limit, 1000),),
            )
            return [row[0] for row in cursor.fetchall()]


class SQLiteMetricsRecorder(_SqliteBase):
    """内存聚合 + 定期快照落盘；重启时恢复累计值（分位数仅本进程内有效）。"""

    def __init__(self, database_path: str) -> None:
        super().__init__(database_path)
        self._memory = InMemoryMetricsRecorder()

    def record(self, trace: CallTrace) -> None:
        self._memory.record(trace)

    def snapshot(self) -> dict[str, Any]:
        return self._memory.snapshot()

    async def flush(self) -> None:
        try:
            await self.initialize()
            payload = json.dumps(self._memory.snapshot(), ensure_ascii=False)
            await asyncio.to_thread(self._upsert, self._database_path, payload)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to flush metrics snapshot")

    async def initialize(self) -> None:
        await super().initialize()
        await asyncio.to_thread(self._restore, self._database_path, self._memory)

    @staticmethod
    def _upsert(database_path: str, payload: str) -> None:
        with _connect(database_path) as conn:
            conn.execute(
                "INSERT INTO metrics_snapshot VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload_json = excluded.payload_json, "
                "updated_at = excluded.updated_at",
                (payload, _now()),
            )
            conn.commit()

    @staticmethod
    def _restore(database_path: str, memory: InMemoryMetricsRecorder) -> None:
        with _connect(database_path) as conn:
            cursor = conn.execute("SELECT payload_json FROM metrics_snapshot WHERE id = 1")
            row = cursor.fetchone()
        if row is None:
            return
        saved = json.loads(row[0])
        memory._requests += saved.get("requests_total", 0)
        memory._errors += saved.get("errors_total", 0)
        for code, count in saved.get("error_codes", {}).items():
            memory._error_codes[code] = memory._error_codes.get(code, 0) + count
        memory._input_tokens += saved.get("tokens", {}).get("input_total", 0)
        memory._output_tokens += saved.get("tokens", {}).get("output_total", 0)


class SQLiteCheckpointStore(_SqliteBase):
    """流检查点落盘：同 request_id 后写覆盖（streaming → 终态流转）。"""

    async def save(
        self, request_id: str, api: str, model: str, status: str, content: str
    ) -> None:
        try:
            await self.initialize()
            await asyncio.to_thread(
                self._upsert, self._database_path, request_id, api, model, status, content
            )
        except Exception:  # noqa: BLE001 —— 检查点写失败不阻断流
            logger.exception("Failed to persist stream checkpoint %s", request_id)

    @staticmethod
    def _upsert(
        database_path: str, request_id: str, api: str, model: str, status: str, content: str
    ) -> None:
        with _connect(database_path) as conn:
            conn.execute(
                "INSERT INTO stream_checkpoints VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(request_id) DO UPDATE SET api = excluded.api, "
                "model = excluded.model, status = excluded.status, "
                "content = excluded.content, updated_at = excluded.updated_at",
                (request_id, api, model, status, content, _now()),
            )
            conn.commit()

    async def get(self, request_id: str) -> dict[str, Any] | None:
        await self.initialize()
        return await asyncio.to_thread(self._select, self._database_path, request_id)

    @staticmethod
    def _select(database_path: str, request_id: str) -> dict[str, Any] | None:
        with _connect(database_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT request_id, api, model, status, content, updated_at "
                "FROM stream_checkpoints WHERE request_id = ?",
                (request_id,),
            )
            row = cursor.fetchone()
        return dict(row) if row else None


class SQLitePromptActivationStore:
    """Prompt 激活表持久化：与审计共库；懒初始化建表。

    同步访问：写入只在管理动作发生，恢复在 create_app 同步装配期，均不在请求路径上。
    """

    def __init__(self, database_path: str) -> None:
        self._database_path = database_path
        self._initialized = False

    def _ensure(self) -> None:
        if self._initialized:
            return
        with _connect(self._database_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS prompt_activations ("
                "name TEXT PRIMARY KEY, version TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.commit()
        self._initialized = True

    def set_active(self, name: str, version: str) -> None:
        try:
            self._ensure()
            with _connect(self._database_path) as conn:
                conn.execute(
                    "INSERT INTO prompt_activations (name, version, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET version = excluded.version, "
                    "updated_at = excluded.updated_at",
                    (name, version, _now()),
                )
                conn.commit()
        except Exception:  # noqa: BLE001 —— 激活持久化失败只记日志
            logger.exception("Failed to persist prompt activation %s=%s", name, version)

    def active_versions(self) -> dict[str, str]:
        try:
            self._ensure()
            with _connect(self._database_path) as conn:
                cursor = conn.execute("SELECT name, version FROM prompt_activations")
                return {row[0]: row[1] for row in cursor.fetchall()}
        except Exception:  # noqa: BLE001
            logger.exception("Failed to read prompt activations")
            return {}
