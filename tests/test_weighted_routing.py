"""S10 加权轮询路由：同 priority 且 cost 相近的候选组内按 weight 轮换首选项。

- 默认关闭：行为与纯 cost 排序一致（回归）。
- 开启后：权重 3:1 的候选在多次请求中首选项分布约为 3:1；其余候选保留为 fallback。
- 显式指定模型不参与轮转；决策留痕记录 weight 与轮转 offset。
"""

import asyncio

import pytest
from pydantic import ValidationError

from app.core.audit import InMemoryAuditStore, RoutingDecisionLog
from app.core.config import AppConfig
from app.core.health import HealthTracker
from app.core.limits import CapacityGuard
from app.core.metrics import InMemoryMetricsRecorder
from app.core.prompts import PromptEngine, PromptRegistry
from app.core.providers.base import ProviderResult
from app.core.router import ModelRouter, RouteRequest
from app.core.schemas import LLMRequest, Message, Usage
from app.service.llm_service import LLMService


def _model(weight: int = 1, *, cost: float = 2.0) -> dict:
    half = cost / 2
    return {
        "adapter": "openai_compatible",
        "capabilities": ["streaming"],
        "priority": 50,
        "weight": weight,
        "provider_model": "m",
        "base_url": "https://x",
        "api_key_env": "K",
        "pricing": {"input": half, "cached_input": half / 10, "output": half},
    }


def _config(weights: dict[str, int], weighted_enabled: bool = True) -> AppConfig:
    return AppConfig.model_validate(
        {
            "version": "1.0",
            "models": {name: _model(w) for name, w in weights.items()},
            "routing": {"weighted_round_robin": {"enabled": weighted_enabled}},
            "limits": {},
            "retry": {},
            "prompts": {},
            "admin_endpoints": {},
            "logging": {},
        }
    )


def _request(model: str | None) -> RouteRequest:
    return RouteRequest(requested_model=model, required_capabilities=frozenset())


def _executable(plans):
    return [p for p in plans if p.reject_reason is None]


def test_model_config_weight_must_be_positive() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _config({"a": 0})
    assert "greater_than_equal" in str(exc_info.value)


def test_weighted_disabled_keeps_cost_ordering() -> None:
    # 默认关闭：同价候选永远是声明顺序的第一个，不轮转。
    config = _config({"provider-a": 3, "provider-b": 1}, weighted_enabled=False)
    router = ModelRouter()
    for _ in range(4):
        plans = router.plan(_request(None), config, HealthTracker())
        assert [p.model for p in _executable(plans)] == ["provider-a", "provider-b"]
        assert all(p.rotation_offset is None for p in plans)


def test_weighted_round_robin_distributes_3_to_1() -> None:
    config = _config({"provider-a": 3, "provider-b": 1})
    router = ModelRouter()
    first_picks = []
    for _ in range(4):
        plans = router.plan(_request(None), config, HealthTracker())
        first_picks.append(_executable(plans)[0].model)
    # 权重 [3,1] 的总池 [a,a,a,b]，连续 offset 0..3 恰好命中 a,a,a,b。
    assert first_picks == ["provider-a", "provider-a", "provider-a", "provider-b"]


def test_rotation_keeps_remaining_candidates_as_fallback_order() -> None:
    config = _config({"provider-a": 3, "provider-b": 1})
    router = ModelRouter()
    # 第 4 次调用轮转到 provider-b：其余候选仍按 cost 升序作为 fallback。
    for _ in range(3):
        router.plan(_request(None), config, HealthTracker())
    plans = router.plan(_request(None), config, HealthTracker())
    assert [p.model for p in _executable(plans)] == ["provider-b", "provider-a"]
    assert [p.order for p in _executable(plans)] == [0, 1]


def test_rotation_offset_and_weight_recorded_on_plan() -> None:
    config = _config({"provider-a": 3, "provider-b": 1})
    router = ModelRouter()
    plans = router.plan(_request(None), config, HealthTracker())
    assert plans[0].weight == 3
    assert plans[0].rotation_offset == 0
    assert plans[1].weight == 1
    assert plans[1].rotation_offset == 0
    plans = router.plan(_request(None), config, HealthTracker())
    assert plans[0].rotation_offset == 1


def test_explicit_model_skips_rotation() -> None:
    # 显式指定模型：确定性优先，不参与轮转。
    config = _config({"provider-a": 3, "provider-b": 1})
    router = ModelRouter()
    plans = router.plan(_request("provider-b"), config, HealthTracker())
    assert [p.model for p in _executable(plans)] == ["provider-b", "provider-a"]
    assert all(p.rotation_offset is None for p in plans)


def test_cost_beyond_tolerance_forms_separate_group() -> None:
    # provider-c 与 a/b 同 priority 但 cost 10.0 超出 10% 容差：独立组，不参与轮转。
    config = AppConfig.model_validate(
        {
            "version": "1.0",
            "models": {
                "provider-a": _model(3, cost=2.0),
                "provider-b": _model(1, cost=2.0),
                "provider-c": _model(1, cost=10.0),
            },
            "routing": {"weighted_round_robin": {"enabled": True}},
            "limits": {},
            "retry": {},
            "prompts": {},
            "admin_endpoints": {},
            "logging": {},
        }
    )
    router = ModelRouter()
    first_picks = []
    for _ in range(4):
        plans = router.plan(_request(None), config, HealthTracker())
        executable = _executable(plans)
        first_picks.append(executable[0].model)
        # 高成本候选始终位于组末。
        assert executable[-1].model == "provider-c"
        assert executable[-1].rotation_offset is None
    assert set(first_picks) == {"provider-a", "provider-b"}
    assert first_picks == ["provider-a", "provider-a", "provider-a", "provider-b"]


class SequenceProvider:
    capabilities = {"streaming"}

    def __init__(self, content: str = "ok") -> None:
        self.content = content

    async def complete(self, config, call) -> ProviderResult:
        return ProviderResult(
            content=self.content,
            usage=Usage(input_tokens=10, output_tokens=5),
            finish_reason="stop",
            provider_request_id="up-1",
        )

    async def stream(self, config, call):
        raise NotImplementedError

    async def close(self) -> None:
        pass


def _service(config, provider):
    return LLMService(
        config=config,
        router=ModelRouter(),
        health=HealthTracker(
            consecutive_failure_threshold=3, cooldown_seconds=30, clock=lambda: 0.0
        ),
        capacity=CapacityGuard(max_concurrent_requests=100, per_model_max_concurrent=20),
        prompts=PromptEngine(PromptRegistry()),
        audit=InMemoryAuditStore(),
        decision_log=RoutingDecisionLog(),
        metrics=InMemoryMetricsRecorder(),
        sleep=asyncio.sleep,
        clock=lambda: 0.0,
        providers={"openai_compatible": provider},
    )


@pytest.mark.asyncio
async def test_decision_log_records_weight_and_rotation_offset() -> None:
    config = _config({"provider-a": 3, "provider-b": 1})
    service = _service(config, SequenceProvider())
    request = LLMRequest(messages=[Message(role="user", content="hi")])
    await service.complete(request)
    await service.complete(request)
    decisions = await service.decision_log.list()
    first = [c for c in decisions[0].candidates if c.order == 0][0]
    assert first.model == "provider-a"
    assert first.weight == 3
    assert first.rotation_offset == 0
    second = [c for c in decisions[1].candidates if c.order == 0][0]
    assert second.model == "provider-a"
    assert second.rotation_offset == 1
