"""路由纯决策：能力过滤 + 健康过滤 + 排序 + 显式模型硬校验。

plan() 返回全部候选（含被拒者）；被拒候选带 reject_reason、order 为 None，
供 RoutingDecision 留痕复盘。加权轮询开启时：同 priority 且 cost 在容差内的
候选组内按 weight 轮换首选项（有状态计数器），显式指定模型时仍保持确定性。
"""

import itertools
from dataclasses import dataclass

from app.core.capabilities import Capability
from app.core.config import AppConfig
from app.core.errors import GatewayError
from app.core.health import HealthTracker


@dataclass(frozen=True)
class RouteRequest:
    requested_model: str | None
    required_capabilities: frozenset[Capability]


@dataclass(frozen=True)
class CandidatePlan:
    model: str
    order: int | None
    priority: int
    cost_per_1m_tokens: float
    reject_reason: str | None
    weight: int = 1
    rotation_offset: int | None = None


class ModelRouter:
    def __init__(self) -> None:
        # 按候选组键控的轮转计数器：同一组多次请求依次加权轮换首选项。
        self._counters: dict[tuple[str, ...], itertools.count] = {}

    def plan(
        self,
        request: RouteRequest,
        config: AppConfig,
        health: HealthTracker,
    ) -> list[CandidatePlan]:
        # 显式模型硬校验：不存在 / 能力不满足 → 400，绝不静默换模型。
        if request.requested_model is not None:
            model_config = config.resolve_model(request.requested_model)
            if model_config is None:
                raise GatewayError(
                    "unknown_model",
                    f"模型 {request.requested_model} 不在 Gateway 允许列表中",
                    400,
                )
            missing = request.required_capabilities - model_config.capabilities
            if missing:
                raise GatewayError(
                    "structured_output_unsupported",
                    "模型不支持请求所需能力",
                    400,
                )

        # 全量候选：能力/健康过滤（记拒绝原因）→ priority 降序 → cost 升序 → 声明顺序。
        candidates: list[CandidatePlan] = []
        for name, model_config in config.models.items():
            missing = request.required_capabilities - model_config.capabilities
            if missing:
                reject_reason = "capability_missing:" + ",".join(sorted(missing))
            elif not health.is_healthy(name):
                reject_reason = "unhealthy"
            else:
                reject_reason = None
            cost = model_config.pricing.input + model_config.pricing.output
            candidates.append(
                CandidatePlan(
                    model=name,
                    order=None,
                    priority=model_config.priority,
                    cost_per_1m_tokens=cost,
                    reject_reason=reject_reason,
                    weight=model_config.weight,
                )
            )

        candidates.sort(
            key=lambda c: (-c.priority, c.cost_per_1m_tokens)
        )  # 稳定排序保留声明顺序

        # requested_model 指定且通过硬校验时排首位（临时不可用仍带 reject_reason 排首位）。
        if request.requested_model is not None:
            candidates.sort(key=lambda c: c.model != request.requested_model)
        elif config.routing.weighted_round_robin.enabled:
            candidates = self._rotate_weighted(
                candidates,
                config.routing.weighted_round_robin.cost_tolerance_ratio,
            )

        # 为可执行候选（无拒绝原因）赋递增顺序号。
        return self._assign_orders(candidates)

    def _rotate_weighted(
        self,
        candidates: list[CandidatePlan],
        cost_tolerance_ratio: float,
    ) -> list[CandidatePlan]:
        # 相同 priority、cost 在容差内的可执行候选归为一组；组内按 weight 加权轮换首选项，
        # 其余候选保留原排序作为 fallback。被拒候选不参与分组，位置不受轮转影响。
        groups: list[list[CandidatePlan]] = []
        for candidate in candidates:
            if candidate.reject_reason is not None:
                continue
            if groups:
                anchor = groups[-1][0]
                same_priority = candidate.priority == anchor.priority
                within_tolerance = (
                    anchor.cost_per_1m_tokens <= 0
                    or (candidate.cost_per_1m_tokens - anchor.cost_per_1m_tokens)
                    / anchor.cost_per_1m_tokens
                    <= cost_tolerance_ratio
                )
                if same_priority and within_tolerance:
                    groups[-1].append(candidate)
                    continue
            groups.append([candidate])

        rotated: dict[str, tuple[list[CandidatePlan], int]] = {}
        for group in groups:
            if len(group) < 2:
                continue
            weighted = [c for c in group for _ in range(c.weight)]
            counter = self._counters.setdefault(
                tuple(c.model for c in group), itertools.count()
            )
            offset = next(counter) % len(weighted)
            primary = weighted[offset]
            ordered = [primary, *[c for c in group if c.model != primary.model]]
            rotated[group[0].model] = (ordered, offset)

        result: list[CandidatePlan] = []
        consumed: set[str] = set()
        for candidate in candidates:
            if candidate.model in consumed:
                continue
            entry = rotated.get(candidate.model)
            if entry is None:
                result.append(candidate)
                continue
            ordered, offset = entry
            result.extend(
                CandidatePlan(
                    model=c.model,
                    order=None,
                    priority=c.priority,
                    cost_per_1m_tokens=c.cost_per_1m_tokens,
                    reject_reason=None,
                    weight=c.weight,
                    rotation_offset=offset,
                )
                for c in ordered
            )
            consumed.update(c.model for c in ordered)
        return result

    def _assign_orders(self, candidates: list[CandidatePlan]) -> list[CandidatePlan]:
        ordered: list[CandidatePlan] = []
        order = 0
        for candidate in candidates:
            if candidate.reject_reason is None:
                ordered.append(
                    CandidatePlan(
                        model=candidate.model,
                        order=order,
                        priority=candidate.priority,
                        cost_per_1m_tokens=candidate.cost_per_1m_tokens,
                        reject_reason=None,
                        weight=candidate.weight,
                        rotation_offset=candidate.rotation_offset,
                    )
                )
                order += 1
            else:
                ordered.append(candidate)
        return ordered
