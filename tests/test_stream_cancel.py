"""S4 流式断开检测：is_disconnected 轮询 → StreamCancelled 终态 + 上游 aclose；
asyncio 取消路径同样补记 cancelled 审计。"""

import asyncio

import pytest

from app.core.audit import InMemoryAuditStore, RoutingDecisionLog
from app.core.health import HealthTracker
from app.core.limits import CapacityGuard
from app.core.metrics import InMemoryMetricsRecorder
from app.core.prompts import PromptEngine, PromptRegistry
from app.core.router import ModelRouter
from app.core.schemas import LLMRequest, Message, StreamCancelled, StreamDelta
from app.service.llm_service import LLMService


class TrackedStreamProvider:
    """流式 Fake：可悬挂（gate）、可观测 aclose（finally 置 closed）。"""

    capabilities = {"streaming", "structured_output"}

    def __init__(
        self,
        chunks: tuple[str, ...] = ("你", "好", "吗"),
        gate: asyncio.Event | None = None,
    ) -> None:
        self.chunks = list(chunks)
        self.gate = gate
        self.first_yielded = asyncio.Event()
        self.closed = False
        self.exhausted = False

    async def complete(self, config, call):
        raise NotImplementedError

    async def stream(self, config, call):
        from app.core.providers.base import StreamChunk

        try:
            for chunk in self.chunks:
                yield StreamChunk(text=chunk, usage=None)
                self.first_yielded.set()
                if self.gate is not None:
                    await self.gate.wait()
            self.exhausted = True  # 自然耗尽（非 aclose）
        finally:
            self.closed = True


def _service(config, provider, audit) -> LLMService:
    return LLMService(
        config=config,
        router=ModelRouter(),
        health=HealthTracker(
            consecutive_failure_threshold=3, cooldown_seconds=30, clock=lambda: 0.0
        ),
        capacity=CapacityGuard(max_concurrent_requests=100, per_model_max_concurrent=20),
        prompts=PromptEngine(PromptRegistry()),
        audit=audit,
        decision_log=RoutingDecisionLog(),
        metrics=InMemoryMetricsRecorder(),
        sleep=asyncio.sleep,
        clock=lambda: 0.0,
        providers={"openai_compatible": provider},
    )


def _request() -> LLMRequest:
    return LLMRequest(messages=[Message(role="user", content="hi")], timeout_seconds=30)


def _disconnect_after(calls_before_disconnect: int):
    """假断开探针：前 N 次返回 False，之后返回 True。"""

    state = {"n": 0}

    async def probe() -> bool:
        state["n"] += 1
        return state["n"] > calls_before_disconnect

    return probe


@pytest.mark.asyncio
async def test_disconnect_emits_cancelled_closes_upstream_and_records_audit(sample_config) -> None:
    provider = TrackedStreamProvider()
    audit = InMemoryAuditStore()
    service = _service(sample_config, provider, audit)

    events = []
    async for event in service.stream(_request(), is_disconnected=_disconnect_after(1)):
        events.append(event)

    # 第一个 delta 转发成功，第二个 delta 转发前探测到断开 → cancelled 终态。
    assert [e.type for e in events] == ["delta", "cancelled"]
    assert isinstance(events[0], StreamDelta)
    assert events[0].text == "你"
    assert isinstance(events[-1], StreamCancelled)
    # 上游迭代器被主动关闭（finally 触发，且未自然耗尽）。
    assert provider.closed is True
    assert provider.exhausted is False
    traces = await audit.list()
    assert len(traces) == 1
    assert traces[0].status == "cancelled"
    assert traces[0].error_code == "client_disconnected"


@pytest.mark.asyncio
async def test_disconnect_never_true_streams_to_completion(sample_config) -> None:
    provider = TrackedStreamProvider()
    audit = InMemoryAuditStore()
    service = _service(sample_config, provider, audit)

    async def always_false() -> bool:
        return False

    events = []
    async for event in service.stream(_request(), is_disconnected=always_false):
        events.append(event)

    assert events[-1].type == "completed"
    assert provider.exhausted is True  # 自然耗尽，无 aclose
    assert (await audit.list())[0].status == "success"


@pytest.mark.asyncio
async def test_client_cancellation_records_cancelled_audit(sample_config) -> None:
    """asyncio 取消（模拟 ASGI 断开）后：CancelledError 继续传播，审计仍补记 cancelled。"""
    gate = asyncio.Event()
    provider = TrackedStreamProvider(gate=gate)
    audit = InMemoryAuditStore()
    service = _service(sample_config, provider, audit)

    async def consume():
        async for _ in service.stream(_request()):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(provider.first_yielded.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # 取消路径的审计经 shield 独立任务落库，让出事件循环让其完成。
    for _ in range(5):
        await asyncio.sleep(0)
    traces = await audit.list()
    assert len(traces) == 1
    assert traces[0].status == "cancelled"
    assert traces[0].error_code == "client_disconnected"
    gate.set()  # 清理：让悬挂的上游生成器完成释放
