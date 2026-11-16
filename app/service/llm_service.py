"""业务编排：路由计划执行、重试/降级、L4 校验、审计触发与流终态状态机。

- complete：候选顺序执行，retryable 错误按 RetryPolicy 重试，fallback_allowed 错误换下一候选；
  L4 校验失败不重试不降级；剩余预算不足 → insufficient_time_budget(429)。
- stream：首块 delta 前允许降级；首块之后任何上游错误只发 failed 终态（终态唯一，绝不重新生成）。
"""

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import uuid4

from app.core.audit import AuditStore, RoutingDecisionLog
from app.core.capabilities import Capability
from app.core.checkpoint import CheckpointStore
from app.core.config import AppConfig, ModelConfig
from app.core.errors import GatewayError
from app.core.health import HealthTracker
from app.core.limits import CapacityGuard
from app.core.logging import call_id_var, trace_id_var
from app.core.metrics import MetricsRecorder, calculate_cost
from app.core.prompts import PromptEngine
from app.core.providers import get_provider
from app.core.providers.base import (
    Provider,
    ProviderCall,
    ProviderError,
    ProviderResult,
)
from app.core.router import ModelRouter, RouteRequest
from app.core.schemas import (
    CallTrace,
    CandidateDecision,
    LLMRequest,
    LLMResponse,
    Message,
    RoutingDecision,
    StreamCancelled,
    StreamCompleted,
    StreamDelta,
    StreamEvent,
    StreamFailed,
    StreamUsageEvent,
    Usage,
)
from app.core.security import identity_var
from app.core.validation import repair_instruction, validate_output
from app.service.retry import (
    RetryPolicy,
    allow_fallback,
    backoff_delay,
    is_retryable_error,
)

logger = logging.getLogger(__name__)


class LLMService:
    def __init__(
        self,
        config: AppConfig,
        router: ModelRouter,
        health: HealthTracker,
        capacity: CapacityGuard,
        prompts: PromptEngine,
        audit: AuditStore,
        decision_log: RoutingDecisionLog,
        metrics: MetricsRecorder,
        sleep=asyncio.sleep,
        clock=time.perf_counter,
        providers: dict[str, Provider] | None = None,
        checkpoint: CheckpointStore | None = None,
    ) -> None:
        self._config = config
        self.router = router
        self.health = health
        self._capacity = capacity
        self.prompts = prompts
        self.audit = audit
        self.decision_log = decision_log
        self.metrics = metrics
        self._sleep = sleep
        self._clock = clock
        self._checkpoint = checkpoint
        self._providers = providers or {}

    def _provider(self, adapter: str) -> Provider:
        provider = self._providers.get(adapter)
        return provider if provider is not None else get_provider(adapter)

    @staticmethod
    def _required_capabilities(request: LLMRequest) -> frozenset[Capability]:
        required: set[Capability] = set()
        if request.stream:
            required.add(Capability.STREAMING)
        if request.response_schema is not None:
            required.add(Capability.STRUCTURED_OUTPUT)
        return frozenset(required)

    async def _prepare(
        self,
        request: LLMRequest,
        call_id: str,
        trace: CallTrace,
        messages: list[Message],
    ):
        if request.prompt is not None:
            applied = self.prompts.apply(request.prompt)
            trace.prompt_name = applied.package.name
            trace.prompt_version = applied.package.version
            trace.prompt_hash = applied.hash
            messages.insert(0, Message(role="system", content=applied.rendered))
        required = self._required_capabilities(request)
        plans = self.router.plan(
            RouteRequest(requested_model=request.model, required_capabilities=required),
            self._config,
            self.health,
        )
        decision = RoutingDecision(
            call_id=call_id,
            timestamp=self._clock(),
            config_version=self._config.version,
            required_capabilities=sorted(required),
            health_snapshot={},
            candidates=[
                CandidateDecision(
                    model=plan.model,
                    order=plan.order,
                    reject_reason=plan.reject_reason,
                    weight=plan.weight,
                    rotation_offset=plan.rotation_offset,
                )
                for plan in plans
            ],
        )
        await self.decision_log.append(decision)
        executable = [plan for plan in plans if plan.reject_reason is None]
        if not executable:
            raise GatewayError("no_compatible_model", "没有可用模型", 503)
        return executable, decision

    @staticmethod
    def _update_candidate(
        decision: RoutingDecision,
        model: str,
        *,
        attempts: int = 0,
        final_error_code: str | None = None,
        capacity_full: bool = False,
    ) -> None:
        for candidate in decision.candidates:
            if candidate.model != model:
                continue
            candidate.attempts += attempts
            if final_error_code:
                candidate.final_error_code = final_error_code
            if capacity_full:
                candidate.reject_reason = "capacity_full"
            return

    async def _record_failure(self, trace: CallTrace, exc: GatewayError, start: float) -> None:
        trace.status = "failed"
        trace.error_code = exc.code
        trace.http_status = exc.status_code
        trace.latency.total_ms = int((self._clock() - start) * 1000)
        await self.audit.append(trace)
        self.metrics.record(trace)

    async def _save_checkpoint(
        self, request_id: str, api: str, model: str, status: str, content: str
    ) -> None:
        if self._checkpoint is None or not self._config.limits.stream_checkpoint.enabled:
            return
        try:
            await self._checkpoint.save(request_id, api, model, status, content)
        except Exception:  # noqa: BLE001 —— 检查点写失败不阻断流式响应
            logger.exception("Failed to persist stream checkpoint %s", request_id)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        start = self._clock()
        trace = CallTrace(
            trace_id=request.trace_id or uuid4().hex,
            call_id=uuid4().hex,
            api_key_hash=identity_var.get(),
            requested_model=request.model,
        )
        # 关联 ID 写入 ContextVar：日志过滤器自动附加，api 层据此回传响应头。
        call_id_var.set(trace.call_id)
        trace_id_var.set(trace.trace_id)
        try:
            return await self._complete(request, trace, start)
        except GatewayError as exc:
            await self._record_failure(trace, exc, start)
            raise

    async def _complete(
        self,
        request: LLMRequest,
        trace: CallTrace,
        start: float,
    ) -> LLMResponse:
        deadline = start + request.timeout_seconds
        trace.resilience.timeout_budget_seconds = request.timeout_seconds
        messages = list(request.messages)
        executable, decision = await self._prepare(request, trace.call_id, trace, messages)

        for plan in executable:
            model_config = self._config.resolve_model(plan.model)
            if model_config is None:
                continue
            if not self._capacity.acquire(plan.model):
                if self._capacity.global_full():
                    raise GatewayError("too_many_requests", "全局并发已满，请稍后重试", 429)
                self._update_candidate(decision, plan.model, capacity_full=True)
                trace.resilience.fallbacks += 1
                continue
            try:
                result, attempts, retries, last_error = await self._attempt_with_retries(
                    plan.model, model_config, request, deadline, messages, trace
                )
            finally:
                self._capacity.release(plan.model)
            if result is None:
                trace.resilience.attempts += attempts
                trace.resilience.retries += retries
                self._update_candidate(
                    decision,
                    plan.model,
                    attempts=attempts,
                    final_error_code=last_error.code if last_error else None,
                )
                if last_error is not None and not allow_fallback(last_error):
                    raise last_error
                trace.resilience.fallbacks += 1
                continue

            parsed = None
            if request.response_schema is not None:
                parsed, repair_attempts, validation_error = await self._validate_with_repair(
                    request, model_config, plan.model, deadline, messages, result, trace
                )
                trace.repair_attempts = repair_attempts
                if validation_error is not None:
                    trace.validation_result = "failed"
                    raise validation_error
                trace.validation_result = "passed"

            trace.actual_model = plan.model
            trace.latency.total_ms = int((self._clock() - start) * 1000)
            trace.resilience.timeout_consumed_seconds = self._clock() - start
            await self.audit.append(trace)
            self.metrics.record(trace)
            return LLMResponse(
                call_id=trace.call_id,
                model=plan.model,
                content=result.content,
                parsed=parsed,
                usage=result.usage,
                latency=trace.latency,
                attempts=trace.resilience.attempts,
            )
        raise GatewayError("model_unavailable", "所有候选模型均不可用", 502)

    async def _attempt_with_retries(
        self,
        model_name: str,
        model_config: ModelConfig,
        request: LLMRequest,
        deadline: float,
        messages: list[Message],
        trace: CallTrace,
    ) -> tuple[ProviderResult | None, int, int, ProviderError | None]:
        retry = self._config.retry
        policy = RetryPolicy(
            max_attempts_per_model=retry.max_attempts_per_model,
            base_delay_seconds=retry.base_delay_seconds,
            backoff_factor=retry.backoff_factor,
            max_delay_seconds=retry.max_delay_seconds,
            jitter=retry.jitter,
        )
        provider = self._provider(model_config.adapter)
        attempts = 0
        retries = 0
        last_error: ProviderError | None = None
        while attempts < policy.max_attempts_per_model:
            remaining = deadline - self._clock()
            if model_config.timeout_seconds is not None:
                # provider 级超时收紧单次上游调用窗口（S12）
                remaining = min(remaining, model_config.timeout_seconds)
            if remaining < self._config.limits.min_remaining_budget_seconds:
                raise GatewayError("insufficient_time_budget", "剩余时间预算不足", 429)
            call = ProviderCall(
                messages=messages,
                timeout_seconds=remaining,
                response_schema=request.response_schema,
            )
            attempts += 1
            try:
                result = await provider.complete(model_config, call)
            except ProviderError as exc:
                last_error = exc
                self.health.record_failure(model_name)
                if not is_retryable_error(exc) or attempts >= policy.max_attempts_per_model:
                    break
                retries += 1
                # sleep 可为同步替身（测试注入 fake_sleep），兼容两种形态。
                delay = self._sleep(backoff_delay(retries, policy))
                if inspect.isawaitable(delay):
                    await delay
                continue
            self.health.record_success(model_name)
            trace.resilience.attempts += attempts
            trace.resilience.retries += retries
            trace.usage = result.usage
            trace.finish_reason = result.finish_reason
            trace.provider = model_config.adapter
            if result.provider_request_id:
                trace.provider_request_ids.append(result.provider_request_id)
            trace.cost = calculate_cost(model_config, result.usage)
            return result, attempts, retries, None
        return None, attempts, retries, last_error

    async def _validate_with_repair(
        self,
        request: LLMRequest,
        model_config: ModelConfig,
        model_name: str,
        deadline: float,
        messages: list[Message],
        result: ProviderResult,
        trace: CallTrace,
    ) -> tuple[Any | None, int, GatewayError | None]:
        # 本地校验；失败且开启 repair 时，把失败输出 + 修复指令作为新消息追加，有界重生成。
        try:
            return validate_output(result.content, request.response_schema), 0, None
        except GatewayError as exc:
            first_error = exc  # except-as 目标在块尾被清除，需转存到普通变量
        repair = self._config.structured_output.repair
        if not repair.enabled:
            return None, 0, first_error
        provider = self._provider(model_config.adapter)
        repair_messages = [*messages, Message(role="assistant", content=result.content)]
        last_error = first_error
        repair_attempts = 0
        for _ in range(repair.max_llm_repair_attempts):
            remaining = deadline - self._clock()
            if model_config.timeout_seconds is not None:
                remaining = min(remaining, model_config.timeout_seconds)
            if remaining < self._config.limits.min_remaining_budget_seconds:
                raise GatewayError("insufficient_time_budget", "剩余时间预算不足", 429)
            repair_attempts += 1
            call = ProviderCall(
                messages=[
                    *repair_messages,
                    Message(
                        role="user",
                        content=repair_instruction(last_error, request.response_schema),
                    ),
                ],
                timeout_seconds=remaining,
                response_schema=request.response_schema,
            )
            try:
                repaired = await provider.complete(model_config, call)
            except ProviderError as exc:
                raise GatewayError(
                    "repair_failed", f"修复调用失败: {exc.message}", exc.status_code
                ) from exc
            self.health.record_success(model_name)
            # 修复调用计入用量与成本，审计可归因。
            trace.usage = Usage(
                input_tokens=trace.usage.input_tokens + repaired.usage.input_tokens,
                output_tokens=trace.usage.output_tokens + repaired.usage.output_tokens,
            )
            trace.cost = calculate_cost(model_config, trace.usage)
            if repaired.provider_request_id:
                trace.provider_request_ids.append(repaired.provider_request_id)
            repair_messages.append(Message(role="assistant", content=repaired.content))
            try:
                return validate_output(repaired.content, request.response_schema), repair_attempts, None
            except GatewayError as exc:
                last_error = exc
        return None, repair_attempts, last_error

    def stream(
        self,
        request: LLMRequest,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
        api: str = "llm",
    ) -> AsyncIterator[StreamEvent]:
        # 工厂方法：同步落定关联 ID（api 层需在响应头发出前读取），返回懒执行生成器。
        start = self._clock()
        trace = CallTrace(
            trace_id=request.trace_id or uuid4().hex,
            call_id=uuid4().hex,
            api_key_hash=identity_var.get(),
            requested_model=request.model,
        )
        call_id_var.set(trace.call_id)
        trace_id_var.set(trace.trace_id)
        deadline = start + request.timeout_seconds
        trace.resilience.timeout_budget_seconds = request.timeout_seconds
        return self._stream_guarded(request, trace, start, deadline, is_disconnected, api)

    async def _stream_guarded(
        self,
        request: LLMRequest,
        trace: CallTrace,
        start: float,
        deadline: float,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
        api: str = "llm",
    ) -> AsyncIterator[StreamEvent]:
        try:
            async for event in self._stream(
                request, trace, start, deadline, is_disconnected, api
            ):
                yield event
        except asyncio.CancelledError:
            # ASGI 断开导致的取消：补记 cancelled 审计后继续传播。
            # shield 保证审计任务独立于取消信号完成。
            if trace.status == "success":
                trace.status = "cancelled"
                trace.error_code = "client_disconnected"
                trace.latency.total_ms = int((self._clock() - start) * 1000)
                await asyncio.shield(self.audit.append(trace))
            raise
        except GatewayError as exc:
            await self._record_failure(trace, exc, start)
            yield StreamFailed(error_code=exc.code, message=exc.message)

    async def _stream(
        self,
        request: LLMRequest,
        trace: CallTrace,
        start: float,
        deadline: float,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
        api: str = "llm",
    ) -> AsyncIterator[StreamEvent]:
        messages = list(request.messages)
        executable, decision = await self._prepare(request, trace.call_id, trace, messages)
        chars_per_token = self._config.prompts.chars_per_token_estimate
        checkpoint_content = ""
        last_checkpoint_flush = start

        for plan in executable:
            model_config = self._config.resolve_model(plan.model)
            if model_config is None:
                continue
            if not self._capacity.acquire(plan.model):
                if self._capacity.global_full():
                    raise GatewayError("too_many_requests", "全局并发已满，请稍后重试", 429)
                self._update_candidate(decision, plan.model, capacity_full=True)
                trace.resilience.fallbacks += 1
                continue
            sent_delta = False
            text_parts: list[str] = []
            real_usage: Usage | None = None
            try:
                remaining = deadline - self._clock()
                if model_config.timeout_seconds is not None:
                    remaining = min(remaining, model_config.timeout_seconds)
                if remaining < self._config.limits.min_remaining_budget_seconds:
                    raise GatewayError("insufficient_time_budget", "剩余时间预算不足", 429)
                call = ProviderCall(
                    messages=messages,
                    timeout_seconds=remaining,
                    response_schema=request.response_schema,
                )
                provider = self._provider(model_config.adapter)
                provider_stream = provider.stream(model_config, call)
                async for chunk in provider_stream:
                    if chunk.usage is not None:
                        # 末块 usage：上游真实统计，覆盖估算值。
                        real_usage = chunk.usage
                        continue
                    text = chunk.text
                    if not text:
                        continue
                    if is_disconnected is not None and await is_disconnected():
                        # 客户端已断开：记 cancelled、关上游迭代、发 cancelled 终态。
                        trace.status = "cancelled"
                        trace.error_code = "client_disconnected"
                        trace.latency.total_ms = int((self._clock() - start) * 1000)
                        await self.audit.append(trace)
                        self.metrics.record(trace)
                        await self._save_checkpoint(
                            trace.call_id, api, plan.model, "cancelled", "".join(text_parts)
                        )
                        await provider_stream.aclose()
                        yield StreamCancelled()
                        return
                    if not sent_delta:
                        trace.latency.ttft_ms = int((self._clock() - start) * 1000)
                        sent_delta = True
                    text_parts.append(text)
                    if (
                        self._checkpoint is not None
                        and self._config.limits.stream_checkpoint.enabled
                    ):
                        checkpoint_content = (checkpoint_content + text)[
                            -self._config.limits.stream_checkpoint.max_chars :
                        ]
                        now = self._clock()
                        if (
                            now - last_checkpoint_flush
                            >= self._config.limits.stream_checkpoint.flush_interval_seconds
                        ):
                            await self._save_checkpoint(
                                trace.call_id, api, plan.model, "streaming", checkpoint_content
                            )
                            last_checkpoint_flush = now
                    yield StreamDelta(text=text)
            except ProviderError as exc:
                self.health.record_failure(plan.model)
                if sent_delta:
                    # 终态唯一：已流出内容后绝不重新生成，只发 failed 终态。
                    trace.status = "failed"
                    trace.error_code = "upstream_stream_failed"
                    trace.http_status = exc.status_code
                    trace.latency.total_ms = int((self._clock() - start) * 1000)
                    await self.audit.append(trace)
                    self.metrics.record(trace)
                    await self._save_checkpoint(
                        trace.call_id, api, plan.model, "error", "".join(text_parts)
                    )
                    yield StreamFailed(
                        error_code="upstream_stream_failed", message=exc.message
                    )
                    return
                self._update_candidate(decision, plan.model, final_error_code=exc.code)
                if not allow_fallback(exc):
                    raise
                trace.resilience.fallbacks += 1
                continue
            finally:
                self._capacity.release(plan.model)

            text = "".join(text_parts)
            estimated = real_usage is None
            usage = real_usage or Usage(
                input_tokens=int(sum(len(m.content) for m in messages) / chars_per_token),
                output_tokens=max(1, int(len(text) / chars_per_token)),
            )
            trace.actual_model = plan.model
            trace.provider = model_config.adapter
            trace.usage = usage
            trace.usage_estimated = estimated
            trace.finish_reason = "stop"
            trace.latency.total_ms = int((self._clock() - start) * 1000)
            trace.resilience.attempts = 1
            await self._save_checkpoint(trace.call_id, api, plan.model, "success", text)
            await self.audit.append(trace)
            self.metrics.record(trace)
            yield StreamUsageEvent(usage=usage, estimated=estimated)
            yield StreamCompleted(model=plan.model, parsed=None, usage=usage)
            return
        raise GatewayError("model_unavailable", "所有候选模型均不可用", 502)

    async def list_traces(self) -> list[CallTrace]:
        return await self.audit.list()

    async def list_routing_decisions(self) -> list[RoutingDecision]:
        return await self.decision_log.list()
