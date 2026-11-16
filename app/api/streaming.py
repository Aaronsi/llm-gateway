"""StreamEvent → 网关 SSE / OpenAI chat.completion.chunk 两种格式化。"""

import json
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from typing import Any

from app.core.schemas import (
    StreamCancelled,
    StreamCompleted,
    StreamDelta,
    StreamEvent,
    StreamFailed,
    StreamUsageEvent,
    Usage,
)


def encode_sse(event: dict[str, Any]) -> str:
    # 将统一事件编码为浏览器和 Agent 都可消费的 SSE 格式。
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def to_gateway_sse(event: StreamEvent) -> str | None:
    # 网关 SSE 协议：content.delta / response.usage / response.completed / response.failed；
    # cancelled 不 emit（连接已断，只写审计）。
    if isinstance(event, StreamDelta):
        return encode_sse({"type": "content.delta", "delta": event.text})
    if isinstance(event, StreamUsageEvent):
        return encode_sse(
            {
                "type": "response.usage",
                "usage": event.usage.model_dump(),
                "estimated": event.estimated,
            }
        )
    if isinstance(event, StreamCompleted):
        return encode_sse(
            {
                "type": "response.completed",
                "model": event.model,
                "parsed": event.parsed,
                "usage": event.usage.model_dump(),
            }
        )
    if isinstance(event, StreamFailed):
        return encode_sse({"type": "response.failed", "error": event.error_code})
    if isinstance(event, StreamCancelled):
        return None
    raise TypeError(f"unknown event: {event!r}")


def _delta_chunk(call_id: str, model: str, text: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }


def _final_chunk(call_id: str, model: str, usage: Usage) -> dict[str, Any]:
    return {
        "id": call_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": usage.input_tokens,
            "completion_tokens": usage.output_tokens,
            "total_tokens": usage.input_tokens + usage.output_tokens,
        },
    }


def _error_chunk(event: StreamFailed) -> dict[str, Any]:
    # OpenAI 形状流式错误事件：稳定 code 映射到 type。
    return {
        "error": {
            "message": event.message or event.error_code,
            "type": event.error_code,
            "param": None,
            "code": None,
        }
    }


async def iter_openai_chunks(
    events: AsyncIterable[StreamEvent],
    call_id: str,
    model: str,
    error_shape: str = "gateway",
) -> AsyncIterator[str]:
    # OpenAI facade 流式懒迭代版：delta chunk + 末块（usage + finish_reason:"stop"）+ [DONE]；
    # 已发首块后的失败默认只断流（无 [DONE]）；openai 形状下追加错误事件 + [DONE]。
    async for event in events:
        if isinstance(event, StreamDelta):
            yield encode_sse(_delta_chunk(call_id, model, event.text))
        elif isinstance(event, StreamUsageEvent):
            continue  # facade 协议中 usage 只在末块携带
        elif isinstance(event, StreamCompleted):
            yield encode_sse(_final_chunk(call_id, model, event.usage))
            yield "data: [DONE]\n\n"
            return
        elif isinstance(event, StreamFailed):
            if error_shape == "openai":
                yield encode_sse(_error_chunk(event))
                yield "data: [DONE]\n\n"
            return
        elif isinstance(event, StreamCancelled):
            return  # 断流，不追加终块


def format_openai_chunks(
    events: Iterable[StreamEvent],
    call_id: str,
    model: str,
    error_shape: str = "gateway",
) -> list[str]:
    # 同步一次性版（测试/缓冲场景）：与 iter_openai_chunks 同构。
    chunks: list[str] = []
    for event in events:
        if isinstance(event, StreamDelta):
            chunks.append(encode_sse(_delta_chunk(call_id, model, event.text)))
        elif isinstance(event, StreamUsageEvent):
            continue  # facade 协议中 usage 只在末块携带
        elif isinstance(event, StreamCompleted):
            chunks.append(encode_sse(_final_chunk(call_id, model, event.usage)))
            chunks.append("data: [DONE]\n\n")
            return chunks
        elif isinstance(event, StreamFailed):
            if error_shape == "openai":
                chunks.append(encode_sse(_error_chunk(event)))
                chunks.append("data: [DONE]\n\n")
            return chunks
        elif isinstance(event, StreamCancelled):
            return chunks  # 断流，不追加终块
    return chunks
