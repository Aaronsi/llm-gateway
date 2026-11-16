"""④ Streaming：SSE 格式化、OpenAI facade chunk、流式 JSON 状态机。"""

import json

import pytest

from app.api.streaming import encode_sse, format_openai_chunks, to_gateway_sse
from app.core.errors import GatewayError
from app.core.schemas import (
    StreamCancelled,
    StreamCompleted,
    StreamDelta,
    StreamFailed,
    StreamUsageEvent,
    Usage,
)
from app.core.validation import IncrementalJsonAccumulator

USAGE = Usage(input_tokens=10, output_tokens=5)


def test_encode_sse_is_data_json_blank_line() -> None:
    sse = encode_sse({"type": "content.delta", "delta": "你好"})
    assert sse == 'data: {"type": "content.delta", "delta": "你好"}\n\n'


def test_delta_maps_to_content_delta() -> None:
    sse = to_gateway_sse(StreamDelta(type="delta", text="hi"))
    assert json.loads(sse.removeprefix("data: ")) == {"type": "content.delta", "delta": "hi"}


def test_usage_event_maps_to_response_usage() -> None:
    sse = to_gateway_sse(StreamUsageEvent(type="usage", usage=USAGE))
    payload = json.loads(sse.removeprefix("data: "))
    assert payload["type"] == "response.usage"
    assert payload["usage"]["input_tokens"] == 10


def test_completed_includes_model_parsed_usage() -> None:
    sse = to_gateway_sse(
        StreamCompleted(type="completed", model="chat-fast", parsed={"a": 1}, usage=USAGE)
    )
    payload = json.loads(sse.removeprefix("data: "))
    assert payload == {
        "type": "response.completed",
        "model": "chat-fast",
        "parsed": {"a": 1},
        "usage": {"input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 0, "reasoning_tokens": 0},
    }


def test_failed_maps_error_code() -> None:
    sse = to_gateway_sse(StreamFailed(type="failed", error_code="upstream_stream_failed"))
    payload = json.loads(sse.removeprefix("data: "))
    assert payload == {"type": "response.failed", "error": "upstream_stream_failed"}


def test_cancelled_emits_nothing() -> None:
    assert to_gateway_sse(StreamCancelled(type="cancelled")) is None


def test_openai_chunks_delta_shape() -> None:
    chunks = format_openai_chunks(
        [StreamDelta(type="delta", text="你")], call_id="call-1", model="chat-fast"
    )
    assert len(chunks) == 1
    chunk = json.loads(chunks[0].removeprefix("data: "))
    assert chunk["object"] == "chat.completion.chunk"
    assert chunk["choices"][0]["delta"] == {"content": "你"}
    assert chunk["choices"][0]["finish_reason"] is None


def test_openai_completed_adds_final_chunk_and_done() -> None:
    events = [
        StreamDelta(type="delta", text="hi"),
        StreamCompleted(type="completed", model="chat-fast", parsed=None, usage=USAGE),
    ]
    chunks = format_openai_chunks(events, call_id="call-1", model="chat-fast")
    assert chunks[-1] == "data: [DONE]\n\n"
    final = json.loads(chunks[-2].removeprefix("data: "))
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["choices"][0]["delta"] == {}
    assert final["usage"]["prompt_tokens"] == 10


def test_openai_failed_stream_truncates_without_done() -> None:
    events = [
        StreamDelta(type="delta", text="hi"),
        StreamFailed(type="failed", error_code="upstream_stream_failed"),
    ]
    chunks = format_openai_chunks(events, call_id="call-1", model="chat-fast")
    assert chunks[-1] != "data: [DONE]\n\n"
    # 已发首块后的失败：只断流，不追加终块
    assert len(chunks) == 1


def test_accumulator_open_fragments_do_not_fail() -> None:
    acc = IncrementalJsonAccumulator()
    for chunk in ['{"answer": ', '"ok', '"}']:
        acc.feed(chunk)
        assert not acc.invalid
    assert acc.final() == {"answer": "ok"}


def test_accumulator_closed_then_malformed_fails_early() -> None:
    acc = IncrementalJsonAccumulator()
    acc.feed('{"answer": }')  # 结构闭合但不可解析
    assert acc.invalid is True
    with pytest.raises(GatewayError) as exc_info:
        acc.final()
    assert exc_info.value.code == "invalid_json"


def test_accumulator_unclosed_at_final_fails() -> None:
    acc = IncrementalJsonAccumulator()
    acc.feed('{"answer": "ok"')
    with pytest.raises(GatewayError) as exc_info:
        acc.final()
    assert exc_info.value.code == "invalid_json"


def test_accumulator_supports_nested_braces_in_strings() -> None:
    acc = IncrementalJsonAccumulator()
    acc.feed('{"a": "含有 } 的字符串", "b": 1}')
    assert acc.final() == {"a": "含有 } 的字符串", "b": 1}
