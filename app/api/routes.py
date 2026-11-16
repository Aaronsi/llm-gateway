"""端点装配：网关原生 /v1/llm、OpenAI facade /v1/chat/completions、管理面端点。

依赖方向单向：routes 只依赖 service 与 api 内协议翻译；装配发生在 app.py。
"""

import time
from collections.abc import AsyncIterable
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.schemas_openai import ChatCompletionRequest, to_openai_response
from app.api.streaming import iter_openai_chunks, to_gateway_sse
from app.core.errors import GatewayError
from app.core.logging import call_id_var, trace_id_var
from app.core.schemas import LLMRequest, PromptActivationRequest, StreamEvent
from app.core.security import authenticate, authenticate_admin
from app.service.llm_service import LLMService


async def limited_identity(
    request: Request, identity: Annotated[str, Depends(authenticate)]
) -> str:
    """认证 + 速率限流组合依赖：先验身份，再按身份桶检查。"""
    await request.app.state.rate_limiter.check(identity)
    return identity


# 认证依赖：配置了 api_keys 则强制校验，否则开放模式（返回客户端指纹）。
Identity = Annotated[str, Depends(limited_identity)]

# 管理端点独立认证：激活端点不共享调用方密钥。
AdminIdentity = Annotated[None, Depends(authenticate_admin)]


def _id_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    call_id = call_id_var.get()
    trace_id = trace_id_var.get()
    if call_id:
        headers["X-Request-Id"] = call_id
    if trace_id:
        headers["X-Trace-Id"] = trace_id
    return headers


async def _gateway_sse_body(events: AsyncIterable[StreamEvent]):
    async for event in events:
        chunk = to_gateway_sse(event)
        if chunk:
            yield chunk


def build_router(service: LLMService) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/llm")
    async def llm(raw: Request, request: LLMRequest, _: Identity):
        if request.stream:
            events = service.stream(request, is_disconnected=raw.is_disconnected, api="llm")
            return StreamingResponse(
                _gateway_sse_body(events),
                media_type="text/event-stream",
                headers=_id_headers(),
            )
        response = await service.complete(request)
        return JSONResponse(content=response.model_dump(), headers=_id_headers())

    @router.post("/v1/llm/stream")
    async def llm_stream(raw: Request, request: LLMRequest, _: Identity):
        request.stream = True
        events = service.stream(request, is_disconnected=raw.is_disconnected, api="llm")
        return StreamingResponse(
            _gateway_sse_body(events),
            media_type="text/event-stream",
            headers=_id_headers(),
        )

    @router.post("/v1/chat/completions")
    async def openai_chat(raw: Request, request: ChatCompletionRequest, _: Identity):
        internal = request.to_internal()
        if internal.stream:
            events = service.stream(
                internal, is_disconnected=raw.is_disconnected, api="chat"
            )
            call_id = call_id_var.get() or ""
            return StreamingResponse(
                iter_openai_chunks(
                    events,
                    call_id,
                    internal.model or "",
                    error_shape=raw.app.state.config.api.error_shape,
                ),
                media_type="text/event-stream",
                headers=_id_headers(),
            )
        response = await service.complete(internal)
        return JSONResponse(
            content=to_openai_response(response, ignored_fields=request.ignored_fields),
            headers=_id_headers(),
        )

    @router.get("/v1/streams/{request_id}/checkpoint")
    async def stream_checkpoint(request_id: str, raw: Request, _: Identity):
        store = getattr(raw.app.state, "checkpoint_store", None)
        checkpoint = await store.get(request_id) if store is not None else None
        if checkpoint is None:
            raise GatewayError(
                "checkpoint_not_found", f"流检查点 {request_id!r} 不存在", 404
            )
        return checkpoint

    @router.get("/v1/models")
    async def models(request: Request, _: Identity):
        now = int(time.time())
        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "created": now, "owned_by": "llm-gateway"}
                for name in sorted(request.app.state.config.models)
            ],
        }

    @router.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    @router.get("/readyz", include_in_schema=False)
    async def readyz(request: Request):
        storage = getattr(request.app.state, "storage", None)
        if storage is not None:
            try:
                if not await storage.ping():
                    return JSONResponse({"status": "unavailable"}, status_code=503)
            except Exception:  # noqa: BLE001 —— 存储异常视为未就绪
                return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready", "service": request.app.state.config.version}

    return router


def build_admin_router(service: LLMService) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/traces")
    async def traces(_: Identity):
        return [trace.model_dump() for trace in await service.list_traces()]

    @router.get("/v1/routing")
    async def routing(_: Identity):
        return [decision.model_dump() for decision in await service.list_routing_decisions()]

    @router.get("/v1/metrics")
    async def metrics(_: Identity):
        return service.metrics.snapshot()

    @router.post("/v1/prompts/activate")
    async def activate_prompt(raw: Request, body: PromptActivationRequest, _: AdminIdentity):
        # 运行时激活：先校验版本存在，再持久化；注册表为进程内共享引用，切换立即生效。
        registry = raw.app.state.prompt_registry
        version = registry.activate(body.name, body.version)
        raw.app.state.prompt_activation_store.set_active(body.name, version)
        return {"name": body.name, "version": version}

    return router
