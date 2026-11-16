"""模型健康状态机：连续失败熔断 + 冷却期后恢复。"""

import time


class HealthTracker:
    def __init__(
        self,
        consecutive_failure_threshold: int = 3,
        cooldown_seconds: float = 30,
        clock=time.monotonic,
    ) -> None:
        self._threshold = consecutive_failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._last_failure_at: dict[str, float] = {}

    def record_success(self, model: str) -> None:
        self._failures[model] = 0

    def record_failure(self, model: str) -> None:
        self._failures[model] = self._failures.get(model, 0) + 1
        self._last_failure_at[model] = self._clock()

    def is_healthy(self, model: str) -> bool:
        failures = self._failures.get(model, 0)
        if failures < self._threshold:
            return True
        last_failure = self._last_failure_at.get(model, 0.0)
        if self._clock() - last_failure > self._cooldown_seconds:
            self._failures[model] = 0
            return True
        return False
