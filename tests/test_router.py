"""② 路由测试：能力过滤、排序、健康过滤、显式模型语义、拒绝原因。

plan() 返回全部候选（含被拒者）：被拒候选 reject_reason 非空、order 为 None；
可执行候选 reject_reason 为 None、order 从 0 递增。
"""

import pytest

from app.core.capabilities import Capability
from app.core.config import AppConfig
from app.core.errors import GatewayError
from app.core.health import HealthTracker
from app.core.router import ModelRouter, RouteRequest


@pytest.fixture
def router() -> ModelRouter:
    return ModelRouter()


def _request(model: str | None, *caps: Capability) -> RouteRequest:
    return RouteRequest(requested_model=model, required_capabilities=frozenset(caps))


def _slim_config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "version": "1.0",
            "models": {
                "slim": {
                    "adapter": "openai_compatible",
                    "capabilities": ["streaming"],
                    "priority": 50,
                    "provider_model": "m",
                    "base_url": "https://x",
                    "api_key_env": "K",
                    "pricing": {"input": 1.0, "cached_input": 0.1, "output": 1.0},
                }
            },
            "limits": {},
            "retry": {},
            "prompts": {},
            "admin_endpoints": {},
            "logging": {},
        }
    )


def _executable(plans) -> list:
    return [p for p in plans if p.reject_reason is None]


def test_sorts_by_priority_desc_then_cost_asc(
    router: ModelRouter, sample_config: AppConfig, health_tracker: HealthTracker
) -> None:
    plans = router.plan(_request(None), sample_config, health_tracker)
    assert [p.model for p in plans] == ["chat-fast", "chat-pro"]
    assert [p.order for p in plans] == [0, 1]
    assert all(p.reject_reason is None for p in plans)


def test_capability_missing_candidate_is_rejected_with_reason(
    router: ModelRouter, health_tracker: HealthTracker
) -> None:
    plans = router.plan(
        _request(None, Capability.STRUCTURED_OUTPUT), _slim_config(), health_tracker
    )
    assert len(plans) == 1
    assert plans[0].reject_reason == "capability_missing:structured_output"
    assert plans[0].order is None
    assert _executable(plans) == []


def test_unhealthy_model_is_rejected_with_reason(
    router: ModelRouter, sample_config: AppConfig, health_tracker: HealthTracker
) -> None:
    for _ in range(3):
        health_tracker.record_failure("chat-fast")
    plans = router.plan(_request(None), sample_config, health_tracker)
    assert plans[0].model == "chat-fast"
    assert plans[0].reject_reason == "unhealthy"
    assert plans[1].model == "chat-pro"
    assert plans[1].reject_reason is None
    assert plans[1].order == 0


def test_all_candidates_rejected_has_no_executable(
    router: ModelRouter, sample_config: AppConfig, health_tracker: HealthTracker
) -> None:
    for model in ("chat-fast", "chat-pro"):
        for _ in range(3):
            health_tracker.record_failure(model)
    plans = router.plan(_request(None), sample_config, health_tracker)
    assert _executable(plans) == []
    assert all(p.reject_reason == "unhealthy" for p in plans)


def test_requested_model_is_placed_first(
    router: ModelRouter, sample_config: AppConfig, health_tracker: HealthTracker
) -> None:
    plans = router.plan(_request("chat-pro"), sample_config, health_tracker)
    assert [p.model for p in plans] == ["chat-pro", "chat-fast"]
    assert [p.order for p in plans] == [0, 1]


def test_requested_unknown_model_raises_unknown_model(
    router: ModelRouter, sample_config: AppConfig, health_tracker: HealthTracker
) -> None:
    with pytest.raises(GatewayError) as exc_info:
        router.plan(_request("ghost"), sample_config, health_tracker)
    assert exc_info.value.code == "unknown_model"
    assert exc_info.value.status_code == 400


def test_requested_model_capability_mismatch_raises_400(
    router: ModelRouter, health_tracker: HealthTracker
) -> None:
    with pytest.raises(GatewayError) as exc_info:
        router.plan(
            _request("slim", Capability.STRUCTURED_OUTPUT), _slim_config(), health_tracker
        )
    assert exc_info.value.code == "structured_output_unsupported"
    assert exc_info.value.status_code == 400


def test_requested_unhealthy_model_still_first_with_reject_reason(
    router: ModelRouter, sample_config: AppConfig, health_tracker: HealthTracker
) -> None:
    for _ in range(3):
        health_tracker.record_failure("chat-fast")
    plans = router.plan(_request("chat-fast"), sample_config, health_tracker)
    assert plans[0].model == "chat-fast"
    assert plans[0].reject_reason == "unhealthy"
    assert plans[0].order is None
    assert plans[1].model == "chat-pro"
    assert plans[1].reject_reason is None
    assert plans[1].order == 0


def test_same_priority_sorted_by_cost_asc(
    router: ModelRouter, health_tracker: HealthTracker
) -> None:
    config = AppConfig.model_validate(
        {
            "version": "1.0",
            "models": {
                "expensive": {
                    "adapter": "openai_compatible",
                    "capabilities": ["streaming"],
                    "priority": 50,
                    "provider_model": "m",
                    "base_url": "https://x",
                    "api_key_env": "K",
                    "pricing": {"input": 2.0, "cached_input": 0.2, "output": 8.0},
                },
                "cheap": {
                    "adapter": "openai_compatible",
                    "capabilities": ["streaming"],
                    "priority": 50,
                    "provider_model": "m",
                    "base_url": "https://x",
                    "api_key_env": "K",
                    "pricing": {"input": 0.5, "cached_input": 0.05, "output": 2.0},
                },
            },
            "limits": {},
            "retry": {},
            "prompts": {},
            "admin_endpoints": {},
            "logging": {},
        }
    )
    plans = router.plan(_request(None), config, health_tracker)
    assert [p.model for p in plans] == ["cheap", "expensive"]
    assert [p.order for p in plans] == [0, 1]


def test_health_tracker_recovers_after_cooldown() -> None:
    from app.core.health import HealthTracker

    now = {"t": 0.0}
    tracker = HealthTracker(
        consecutive_failure_threshold=3, cooldown_seconds=30, clock=lambda: now["t"]
    )
    for _ in range(3):
        tracker.record_failure("chat-fast")
    assert not tracker.is_healthy("chat-fast")
    now["t"] = 31.0  # 冷却期后
    assert tracker.is_healthy("chat-fast")


def test_health_tracker_success_resets_failures(health_tracker: HealthTracker) -> None:
    health_tracker.record_failure("chat-fast")
    health_tracker.record_failure("chat-fast")
    health_tracker.record_success("chat-fast")
    health_tracker.record_failure("chat-fast")
    health_tracker.record_failure("chat-fast")
    assert health_tracker.is_healthy("chat-fast")
