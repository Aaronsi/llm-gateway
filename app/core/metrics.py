"""分项成本计算与 Metrics 聚合（由 CallTrace 派生，不重复采集）。"""

import math
from typing import Any, Protocol

from app.core.config import ModelConfig
from app.core.schemas import CallTrace, CostBreakdown, Usage


def calculate_cost(model_config: ModelConfig, usage: Usage) -> CostBreakdown:
    pricing = model_config.pricing
    input_usd = usage.input_tokens * pricing.input / 1_000_000
    cached_usd = usage.cached_input_tokens * pricing.cached_input / 1_000_000
    output_usd = usage.output_tokens * pricing.output / 1_000_000
    return CostBreakdown(
        input_usd=input_usd,
        cached_usd=cached_usd,
        output_usd=output_usd,
        total_usd=input_usd + cached_usd + output_usd,
    )


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    index = max(0, math.ceil(p * len(sorted_values)) - 1)
    return sorted_values[index]


class MetricsRecorder(Protocol):
    def record(self, trace: CallTrace) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...


class InMemoryMetricsRecorder:
    def __init__(self) -> None:
        self._requests = 0
        self._errors = 0
        self._error_codes: dict[str, int] = {}
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_ms: list[float] = []
        self._ttft_ms: list[float] = []

    def record(self, trace: CallTrace) -> None:
        self._requests += 1
        if trace.status != "success":
            self._errors += 1
        if trace.error_code:
            self._error_codes[trace.error_code] = self._error_codes.get(trace.error_code, 0) + 1
        self._input_tokens += trace.usage.input_tokens
        self._output_tokens += trace.usage.output_tokens
        self._total_ms.append(float(trace.latency.total_ms))
        if trace.latency.ttft_ms is not None:
            self._ttft_ms.append(float(trace.latency.ttft_ms))

    def snapshot(self) -> dict[str, Any]:
        total_ms = sorted(self._total_ms)
        ttft_ms = sorted(self._ttft_ms)
        return {
            "requests_total": self._requests,
            "errors_total": self._errors,
            "error_rate": self._errors / self._requests if self._requests else 0.0,
            "error_codes": dict(self._error_codes),
            "tokens": {
                "input_total": self._input_tokens,
                "output_total": self._output_tokens,
            },
            "latency_ms": {
                "p50": _percentile(total_ms, 0.5),
                "p95": _percentile(total_ms, 0.95),
                "p99": _percentile(total_ms, 0.99),
                "ttft_p50": _percentile(ttft_ms, 0.5),
            },
        }
