"""容量守卫：全局 + 每模型并发池。"""


class CapacityGuard:
    def __init__(
        self,
        max_concurrent_requests: int = 100,
        per_model_max_concurrent: int = 20,
    ) -> None:
        self._global_limit = max_concurrent_requests
        self._per_model_limit = per_model_max_concurrent
        self._global_in_use = 0
        self._model_in_use: dict[str, int] = {}

    def acquire(self, model: str) -> bool:
        # 全局池或模型池已满 → False；否则占用并返回 True。
        if self._global_in_use >= self._global_limit:
            return False
        if self._model_in_use.get(model, 0) >= self._per_model_limit:
            return False
        self._global_in_use += 1
        self._model_in_use[model] = self._model_in_use.get(model, 0) + 1
        return True

    def release(self, model: str) -> None:
        if self._model_in_use.get(model, 0) > 0:
            self._model_in_use[model] -= 1
        if self._global_in_use > 0:
            self._global_in_use -= 1

    def global_full(self) -> bool:
        # 区分「模型池满（换候选）」与「全局满（429）」。
        return self._global_in_use >= self._global_limit
