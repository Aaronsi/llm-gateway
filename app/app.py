"""应用装配：唯一允许跨层 wiring 的地方（依赖方向 api → service → core 单向）。"""

import asyncio
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI

from app.api.errors import gateway_error_handler
from app.api.routes import build_admin_router, build_router
from app.core.audit import InMemoryAuditStore, RoutingDecisionLog
from app.core.checkpoint import InMemoryCheckpointStore
from app.core.config import AppConfig
from app.core.errors import GatewayError
from app.core.health import HealthTracker
from app.core.limits import CapacityGuard
from app.core.metrics import InMemoryMetricsRecorder
from app.core.prompt_activation import InMemoryPromptActivationStore
from app.core.prompts import PromptEngine, PromptRegistry
from app.core.providers import close_all_providers
from app.core.providers.base import Provider
from app.core.rate_limit import InMemoryRateLimiter
from app.core.router import ModelRouter
from app.core.storage import (
    SQLiteAuditStore,
    SQLiteCheckpointStore,
    SQLiteMetricsRecorder,
    SQLitePromptActivationStore,
    SQLiteRoutingDecisionLog,
)
from app.service.llm_service import LLMService


def _build_stores(config: AppConfig):
    """按 storage.backend 选择审计/决策留痕/指标/检查点/激活表实现；sqlite 组件共库。"""
    if config.storage.backend == "sqlite":
        path = config.storage.database_path
        return (
            SQLiteAuditStore(path),
            SQLiteRoutingDecisionLog(path),
            SQLiteMetricsRecorder(path),
            SQLiteCheckpointStore(path),
            SQLitePromptActivationStore(path),
        )
    return (
        InMemoryAuditStore(),
        RoutingDecisionLog(),
        InMemoryMetricsRecorder(),
        InMemoryCheckpointStore(),
        InMemoryPromptActivationStore(),
    )


def create_app(
    config: AppConfig | None = None,
    providers: dict[str, Provider] | None = None,
    prompts_dir: str | Path | None = None,
    sleep=asyncio.sleep,
    clock=time.perf_counter,
) -> FastAPI:
    """组装 FastAPI 应用；测试可注入 providers（FakeProvider）与 prompts_dir。

    config 缺省时为进程引导模式（uvicorn --factory 零参调用）：从 GATEWAY_CONFIG
    环境变量（缺省 config.yaml）加载配置，并自动加载当前目录 prompts/（存在时）。
    """
    env_boot = config is None
    if env_boot:
        config = AppConfig.load(os.environ.get("GATEWAY_CONFIG", "config.yaml"))
        if prompts_dir is None:
            default_prompts = Path("prompts")
            prompts_dir = default_prompts if default_prompts.is_dir() else None
    registry = PromptRegistry.load(prompts_dir) if prompts_dir else PromptRegistry()
    audit, decision_log, metrics, checkpoint_store, activation_store = _build_stores(config)
    # 启动时从持久层恢复激活版本索引（同步装配期）；未知版本组合在 registry 内过滤。
    registry.restore_active(activation_store.active_versions())
    service = LLMService(
        config=config,
        router=ModelRouter(),
        health=HealthTracker(
            consecutive_failure_threshold=config.routing.health.consecutive_failure_threshold,
            cooldown_seconds=config.routing.health.cooldown_seconds,
            clock=clock,
        ),
        capacity=CapacityGuard(
            max_concurrent_requests=config.limits.max_concurrent_requests,
            per_model_max_concurrent=config.limits.per_model_max_concurrent,
        ),
        prompts=PromptEngine(registry, config.prompts.chars_per_token_estimate),
        audit=audit,
        decision_log=decision_log,
        metrics=metrics,
        sleep=sleep,
        clock=clock,
        providers=providers,
        checkpoint=checkpoint_store,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """初始化存储、启动指标定期落盘；退出时冲刷指标并关闭供应商连接池。"""
        await audit.initialize() if isinstance(audit, SQLiteAuditStore) else None
        flush_task = None
        if isinstance(metrics, SQLiteMetricsRecorder):
            flush_task = asyncio.create_task(
                _metrics_flush_loop(
                    metrics,
                    config.storage.metrics_flush_interval_seconds,
                    sleep,
                )
            )
        try:
            yield
        finally:
            if flush_task is not None:
                flush_task.cancel()
                with suppress(asyncio.CancelledError):
                    await flush_task
            if isinstance(metrics, SQLiteMetricsRecorder):
                await metrics.flush()
            await close_all_providers()

    async def _metrics_flush_loop(recorder, interval: float, sleep) -> None:
        while True:
            await sleep(interval)
            await recorder.flush()

    app = FastAPI(title="llm-gateway", version=config.version, lifespan=lifespan)
    app.state.config = config
    app.state.rate_limiter = InMemoryRateLimiter(config.limits.rate)
    app.state.storage = audit
    app.state.checkpoint_store = checkpoint_store
    app.state.prompt_registry = registry
    app.state.prompt_activation_store = activation_store
    app.include_router(build_router(service))
    if config.admin_endpoints.enabled:
        app.include_router(build_admin_router(service))
    app.add_exception_handler(GatewayError, gateway_error_handler)
    return app
