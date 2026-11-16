"""S6 流检查点：按间隔落盘、终态流转 streaming→success/cancelled、默认关闭、API 查询。"""

import asyncio

import pytest

from app.core.audit import InMemoryAuditStore, RoutingDecisionLog
from app.core.checkpoint import InMemoryCheckpointStore
from app.core.config import AppConfig
from app.core.health import HealthTracker
from app.core.limits import CapacityGuard
from app.core.logging import call_id_var
from app.core.metrics import InMemoryMetricsRecorder
from app.core.prompts import PromptEngine, PromptRegistry
from app.core.providers.base import StreamChunk
from app.core.router import ModelRouter
from app.core.schemas import LLMRequest, Message
from app.service.llm_service import LLMService

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


class TextProvider:
    capabilities = {"streaming", "structured_output"}

    async def complete(self, config, call):
        raise NotImplementedError

    async def stream(self, config, call):
        yield StreamChunk(text="你", usage=None)
        yield StreamChunk(text="好", usage=None)

    async def close(self) -> None:
        pass


class ManualClock:
    """可手动推进的时钟：检查点间隔判定用。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _service(config, checkpoint_store, clock):
    return LLMService(
        config=config,
        router=ModelRouter(),
        health=HealthTracker(
            consecutive_failure_threshold=3, cooldown_seconds=30, clock=clock
        ),
        capacity=CapacityGuard(max_concurrent_requests=100, per_model_max_concurrent=20),
        prompts=PromptEngine(PromptRegistry()),
        audit=InMemoryAuditStore(),
        decision_log=RoutingDecisionLog(),
        metrics=InMemoryMetricsRecorder(),
        checkpoint=checkpoint_store,
        sleep=asyncio.sleep,
        clock=clock,
        providers={"openai_compatible": TextProvider()},
    )


def _request() -> LLMRequest:
    return LLMRequest(messages=[Message(role="user", content="hi")], timeout_seconds=30)


@pytest.mark.asyncio
async def test_checkpoint_flushes_by_interval_then_terminal_success() -> None:
    config = make_config(
        limits={"stream_checkpoint": {"enabled": True, "flush_interval_seconds": 1.0}}
    )
    store = InMemoryCheckpointStore()
    clock = ManualClock()
    service = _service(config, store, clock)

    events = service.stream(_request())
    call_id = call_id_var.get()
    first = await anext(events)  # t=0 delta1：未到间隔，不落盘
    assert store._rows == {}
    clock.t += 1.0
    second = await anext(events)  # t=1 delta2：触发 streaming 落盘
    row = await store.get(call_id)
    assert row is not None
    assert row["status"] == "streaming"
    assert row["content"] == "你好"
    assert first.text == "你" and second.text == "好"
    async for _ in events:  # 消费到终态
        pass
    final = await store.get(call_id)
    assert final["status"] == "success"
    assert final["content"] == "你好"


@pytest.mark.asyncio
async def test_checkpoint_disabled_by_default_writes_nothing() -> None:
    config = make_config()  # 默认 enabled=False
    store = InMemoryCheckpointStore()
    service = _service(config, store, ManualClock())

    events = service.stream(_request())
    call_id = call_id_var.get()
    async for _ in events:
        pass
    assert await store.get(call_id) is None


@pytest.mark.asyncio
async def test_checkpoint_cancelled_status_saved() -> None:
    from app.core.schemas import StreamCancelled

    config = make_config(
        limits={"stream_checkpoint": {"enabled": True, "flush_interval_seconds": 100.0}}
    )
    store = InMemoryCheckpointStore()
    service = _service(config, store, ManualClock())

    state = {"n": 0}

    async def disconnected() -> bool:
        state["n"] += 1
        return state["n"] > 1  # 第二个 delta 转发前断开

    events = service.stream(_request(), is_disconnected=disconnected)
    call_id = call_id_var.get()
    collected = [event async for event in events]
    assert isinstance(collected[-1], StreamCancelled)
    row = await store.get(call_id)
    assert row["status"] == "cancelled"
    assert row["content"] == "你"  # 断开前已接收的第一个增量


@pytest.mark.asyncio
async def test_api_checkpoint_query_and_survives_restart(tmp_path, make_client) -> None:
    path = str(tmp_path / "gateway.db")
    config = make_config(
        storage={"backend": "sqlite", "database_path": path},
        limits={"stream_checkpoint": {"enabled": True, "flush_interval_seconds": 0}},
        admin_endpoints={"enabled": True},
    )
    async with make_client(config=config) as client:
        response = await client.post(
            "/v1/llm",
            json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        request_id = response.headers["x-request-id"]
        checkpoint = (await client.get(f"/v1/streams/{request_id}/checkpoint")).json()
    assert checkpoint["status"] == "success"
    assert checkpoint["content"] == "你好"
    assert checkpoint["model"] == "chat-fast"

    # 重启（同路径新 app）后检查点仍在
    async with make_client(config=config) as client:
        again = (await client.get(f"/v1/streams/{request_id}/checkpoint")).json()
    assert again["content"] == "你好"


@pytest.mark.asyncio
async def test_checkpoint_not_found_returns_404(make_client) -> None:
    async with make_client() as client:
        response = await client.post(
            "/v1/llm",
            json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        request_id = response.headers["x-request-id"]
        missing = await client.get(f"/v1/streams/{request_id}/checkpoint")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "checkpoint_not_found"
