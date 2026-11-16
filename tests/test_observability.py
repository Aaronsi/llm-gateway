"""⑥ 可观测性：CallTrace 全字段、Metrics 快照、分项成本、审计存储、路由决策留痕。"""

from datetime import UTC, datetime

import pytest

from app.core.audit import InMemoryAuditStore, RoutingDecisionLog
from app.core.config import ModelConfig
from app.core.metrics import InMemoryMetricsRecorder, calculate_cost
from app.core.schemas import (
    CallTrace,
    CandidateDecision,
    CostBreakdown,
    LatencyBreakdown,
    RoutingDecision,
    Usage,
)


def _model_config() -> ModelConfig:
    return ModelConfig(
        adapter="openai_compatible",
        capabilities=set(),
        priority=100,
        provider_model="m",
        base_url="https://x",
        api_key_env="K",
        pricing={"input": 1.0, "cached_input": 0.1, "output": 4.0},
    )


def _trace(call_id: str, status: str = "success", error_code: str | None = None) -> CallTrace:
    usage = Usage(input_tokens=1_000_000, cached_input_tokens=500_000, output_tokens=250_000)
    return CallTrace(
        trace_id="t1",
        call_id=call_id,
        requested_model="chat-fast",
        actual_model="chat-fast",
        provider="openai_compatible",
        routing_decision_id="rd-1",
        prompt_name="knowledge_decision",
        prompt_version="v1",
        prompt_hash="h",
        usage=usage,
        latency=LatencyBreakdown(queue_ms=1, route_ms=2, ttft_ms=100, generation_ms=900, total_ms=1000),
        cost=CostBreakdown(input_usd=1.0, cached_usd=0.05, output_usd=1.0, total_usd=2.05),
        finish_reason="stop",
        status=status,
        validation_result="passed",
        error_code=error_code,
        http_status=200 if status == "success" else 502,
        provider_request_ids=["up-1"],
    )


def test_calculate_cost_breakdown_per_item() -> None:
    usage = Usage(input_tokens=1_000_000, cached_input_tokens=500_000, output_tokens=250_000)
    cost = calculate_cost(_model_config(), usage)
    assert cost.input_usd == 1.0
    assert cost.cached_usd == pytest.approx(0.05)
    assert cost.output_usd == 1.0
    assert cost.total_usd == pytest.approx(2.05)


def test_calculate_cost_zero_usage_is_zero() -> None:
    cost = calculate_cost(_model_config(), Usage())
    assert cost.total_usd == 0.0


@pytest.mark.asyncio
async def test_in_memory_audit_store_appends_and_lists() -> None:
    store = InMemoryAuditStore()
    trace = _trace("c1")
    await store.append(trace)
    assert await store.list() == [trace]


@pytest.mark.asyncio
async def test_routing_decision_log_appends_and_lists() -> None:
    log = RoutingDecisionLog()
    decision = RoutingDecision(
        call_id="c1",
        timestamp=datetime.now(UTC),
        config_version="1.0",
        required_capabilities=["streaming"],
        health_snapshot={"chat-fast": "healthy"},
        candidates=[
            CandidateDecision(model="chat-fast", order=0, selected=True, attempts=1),
            CandidateDecision(model="chat-pro", order=1, selected=False, attempts=0),
        ],
    )
    await log.append(decision)
    listed = await log.list()
    assert len(listed) == 1
    assert listed[0].candidates[0].selected is True
    assert listed[0].candidates[0].order == 0


def test_metrics_recorder_aggregates_totals() -> None:
    recorder = InMemoryMetricsRecorder()
    recorder.record(_trace("c1"))
    recorder.record(_trace("c2"))
    snapshot = recorder.snapshot()
    assert snapshot["requests_total"] == 2
    assert snapshot["errors_total"] == 0
    assert snapshot["error_rate"] == 0.0
    assert snapshot["tokens"]["input_total"] == 2_000_000
    assert snapshot["tokens"]["output_total"] == 500_000


def test_metrics_recorder_counts_errors_and_codes() -> None:
    recorder = InMemoryMetricsRecorder()
    recorder.record(_trace("c1", status="failed", error_code="model_unavailable"))
    recorder.record(_trace("c2"))
    recorder.record(_trace("c3", status="failed", error_code="model_unavailable"))
    snapshot = recorder.snapshot()
    assert snapshot["errors_total"] == 2
    assert snapshot["error_rate"] == 2 / 3
    assert snapshot["error_codes"] == {"model_unavailable": 2}


def test_metrics_recorder_computes_latency_percentiles() -> None:
    recorder = InMemoryMetricsRecorder()
    for i in range(10):
        trace = _trace(f"c{i}")
        trace.latency = LatencyBreakdown(total_ms=(i + 1) * 10, ttft_ms=i + 1)
        recorder.record(trace)
    snapshot = recorder.snapshot()
    assert snapshot["latency_ms"]["p50"] == 50
    assert snapshot["latency_ms"]["p99"] == 100
    assert snapshot["latency_ms"]["ttft_p50"] == 5


def test_trace_whitelist_excludes_content_fields() -> None:
    # CallTrace schema 即白名单：不含 prompt 正文与模型回答内容字段。
    fields = set(CallTrace.model_fields)
    assert "content" not in fields
    assert "prompt_text" not in fields
    assert "messages" not in fields
