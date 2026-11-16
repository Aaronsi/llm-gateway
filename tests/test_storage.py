"""S3 SQLite 持久化：审计/决策留痕/指标快照跨重启存活，写失败不阻断请求。

白名单原则：只落 CallTrace 已声明字段（不含正文）；指标分位数为进程内聚合，
重启后恢复请求数/错误/Token 累计。
"""

import pytest

from app.core.config import AppConfig
from app.core.schemas import CallTrace, CandidateDecision, RoutingDecision, Usage
from app.core.storage import (
    SQLiteAuditStore,
    SQLiteMetricsRecorder,
    SQLiteRoutingDecisionLog,
)

MODELS = {
    "chat-fast": {
        "adapter": "openai_compatible",
        "capabilities": ["streaming", "structured_output"],
        "priority": 100,
        "provider_model": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "structured_output_mode": "json_object",
        "pricing": {"input": 1.0, "cached_input": 0.1, "output": 4.0},
    },
}


def make_config(**overrides) -> AppConfig:
    raw = {"version": "1.0", "models": MODELS}
    raw.update(overrides)
    return AppConfig.model_validate(raw)


def make_trace(call_id: str = "c1") -> CallTrace:
    return CallTrace(
        trace_id="t1",
        call_id=call_id,
        api_key_hash="abc123",
        requested_model="chat-fast",
        actual_model="chat-fast",
        status="success",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def make_decision(call_id: str = "c1") -> RoutingDecision:
    return RoutingDecision(
        call_id=call_id,
        config_version="1.0",
        candidates=[CandidateDecision(model="chat-fast", order=0, selected=True, attempts=1)],
    )


@pytest.mark.asyncio
async def test_audit_append_and_list_roundtrip(tmp_path) -> None:
    store = SQLiteAuditStore(str(tmp_path / "gateway.db"))
    await store.append(make_trace("c1"))
    await store.append(make_trace("c2"))
    listed = await store.list()
    assert [t.call_id for t in listed] == ["c2", "c1"]  # 最近优先
    assert listed[0].api_key_hash == "abc123"
    assert listed[0].usage.input_tokens == 10


@pytest.mark.asyncio
async def test_audit_survives_restart(tmp_path) -> None:
    path = str(tmp_path / "gateway.db")
    first = SQLiteAuditStore(path)
    await first.append(make_trace("c1"))
    second = SQLiteAuditStore(path)  # 模拟重启
    listed = await second.list()
    assert [t.call_id for t in listed] == ["c1"]


@pytest.mark.asyncio
async def test_audit_append_failure_is_swallowed(tmp_path) -> None:
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("file")
    store = SQLiteAuditStore(str(blocker / "gateway.db"))  # 父路径是文件，建库必败
    await store.append(make_trace("c1"))  # 不抛异常


@pytest.mark.asyncio
async def test_decision_log_roundtrip_and_restart(tmp_path) -> None:
    path = str(tmp_path / "gateway.db")
    log = SQLiteRoutingDecisionLog(path)
    await log.append(make_decision("c1"))
    listed = await log.list()
    assert len(listed) == 1
    assert listed[0].candidates[0].selected is True
    again = SQLiteRoutingDecisionLog(path)
    assert len(await again.list()) == 1


@pytest.mark.asyncio
async def test_metrics_flush_and_restore(tmp_path) -> None:
    path = str(tmp_path / "gateway.db")
    recorder = SQLiteMetricsRecorder(path)
    recorder.record(make_trace("c1"))
    recorder.record(make_trace("c2"))
    await recorder.flush()

    restored = SQLiteMetricsRecorder(path)
    await restored.initialize()
    snapshot = restored.snapshot()
    assert snapshot["requests_total"] == 2
    assert snapshot["tokens"]["input_total"] == 20


@pytest.mark.asyncio
async def test_ping_reports_health(tmp_path) -> None:
    healthy = SQLiteAuditStore(str(tmp_path / "gateway.db"))
    assert await healthy.ping() is True
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("file")
    broken = SQLiteAuditStore(str(blocker / "gateway.db"))
    assert await broken.ping() is False


@pytest.mark.asyncio
async def test_api_uses_sqlite_backend_and_survives_restart(tmp_path, make_client) -> None:
    path = str(tmp_path / "gateway.db")
    config = make_config(
        storage={"backend": "sqlite", "database_path": path},
        admin_endpoints={"enabled": True},
    )
    async with make_client(config=config) as client:
        response = await client.post(
            "/v1/llm", json={"messages": [{"role": "user", "content": "hi"}]}
        )
        assert response.status_code == 200
        traces = (await client.get("/v1/traces")).json()
    assert len(traces) == 1
    assert traces[0]["actual_model"] == "chat-fast"

    # 重启（同路径新 app）后审计仍在
    async with make_client(config=config) as client:
        traces = (await client.get("/v1/traces")).json()
    assert len(traces) == 1
