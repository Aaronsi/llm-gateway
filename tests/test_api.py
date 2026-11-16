"""网关原生端点：/v1/llm、/v1/llm/stream、管理面开关、错误映射、关联 ID 响应头。

用例由基线 test_gateway.py 按新契约迁移：模型白名单、Structured Output、重试降级、
SSE、未知字段 422、审计查询均保留；响应体新增 call_id/latency 等字段。
"""

import json

import pytest

from app.core.providers.base import ProviderResult, ProviderTimeoutError
from app.core.schemas import Usage


class FakeProvider:
    capabilities = {"streaming", "structured_output"}

    def __init__(self, content: str = "ok", failures: int = 0) -> None:
        self.content = content
        self.failures = failures

    async def complete(self, config, call) -> ProviderResult:
        if self.failures > 0:
            self.failures -= 1
            raise ProviderTimeoutError()
        return ProviderResult(
            content=self.content,
            usage=Usage(input_tokens=10, output_tokens=5),
            finish_reason="stop",
            provider_request_id="up-1",
        )

    async def stream(self, config, call):
        yield "你"
        yield "好"

    async def close(self) -> None:
        pass


def _write_prompt_assets(prompts_dir) -> None:
    (prompts_dir / "knowledge_decision").mkdir(parents=True, exist_ok=True)
    (prompts_dir / "knowledge_decision" / "v1.yaml").write_text(
        "name: knowledge_decision\n"
        "version: v1\n"
        "status: published\n"
        "system_template: \"你是{{ product_name }}的决策器。\"\n"
        "variables:\n"
        "  product_name: {type: str, max_length: 50}\n"
        "context_budget: 2000\n",
        encoding="utf-8",
    )


SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


@pytest.mark.asyncio
async def test_llm_success_and_id_headers(make_client) -> None:
    async with make_client() as client:
        response = await client.post(
            "/v1/llm",
            json={"model": "chat-fast", "messages": [{"role": "user", "content": "hello"}]},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == "chat-fast"
    assert body["content"] == "ok"
    assert body["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
    }
    assert body["attempts"] == 1
    assert response.headers["x-request-id"] == body["call_id"]
    assert response.headers["x-trace-id"]


@pytest.mark.asyncio
async def test_llm_structured_output_success(make_client) -> None:
    provider = FakeProvider(content=json.dumps({"answer": "ok"}))
    async with make_client(provider=provider) as client:
        response = await client.post(
            "/v1/llm",
            json={
                "model": "chat-fast",
                "messages": [{"role": "user", "content": "s"}],
                "response_schema": SCHEMA,
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["parsed"] == {"answer": "ok"}


@pytest.mark.asyncio
async def test_llm_structured_output_invalid_json_502(make_client) -> None:
    async with make_client() as client:  # 默认 content="ok" 非 JSON
        response = await client.post(
            "/v1/llm",
            json={
                "model": "chat-fast",
                "messages": [{"role": "user", "content": "s"}],
                "response_schema": SCHEMA,
            },
        )
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "invalid_json"


@pytest.mark.asyncio
async def test_llm_unknown_model_400(make_client) -> None:
    async with make_client() as client:
        response = await client.post(
            "/v1/llm",
            json={"model": "ghost", "messages": [{"role": "user", "content": "s"}]},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unknown_model"


@pytest.mark.asyncio
async def test_llm_missing_prompt_variable_400(make_client, tmp_path) -> None:
    _write_prompt_assets(tmp_path)
    async with make_client(prompts_dir=tmp_path) as client:
        response = await client.post(
            "/v1/llm",
            json={
                "model": "chat-fast",
                "messages": [{"role": "user", "content": "s"}],
                "prompt": {"name": "knowledge_decision", "version": "v1", "variables": {}},
            },
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "missing_prompt_variable"


@pytest.mark.asyncio
async def test_llm_unknown_field_422(make_client) -> None:
    async with make_client() as client:
        response = await client.post(
            "/v1/llm",
            json={
                "model": "chat-fast",
                "messages": [{"role": "user", "content": "s"}],
                "unexpected": True,
            },
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_llm_stream_endpoint_sse(make_client) -> None:
    async with make_client() as client:
        response = await client.post(
            "/v1/llm/stream",
            json={"model": "chat-fast", "messages": [{"role": "user", "content": "s"}]},
        )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type": "content.delta"' in response.text
    assert '"type": "response.completed"' in response.text


@pytest.mark.asyncio
async def test_llm_stream_flag_sse(make_client) -> None:
    async with make_client() as client:
        response = await client.post(
            "/v1/llm",
            json={"model": "chat-fast", "stream": True, "messages": [{"role": "user", "content": "s"}]},
        )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type": "response.completed"' in response.text


@pytest.mark.asyncio
async def test_retry_then_fallback(make_client) -> None:
    async with make_client(provider=FakeProvider(failures=2)) as client:
        response = await client.post(
            "/v1/llm",
            json={"model": "chat-fast", "messages": [{"role": "user", "content": "retry"}]},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == "chat-pro"
    assert body["attempts"] == 3


@pytest.mark.asyncio
async def test_admin_endpoints_disabled_by_default(make_client) -> None:
    async with make_client() as client:
        assert (await client.get("/v1/traces")).status_code == 404
        assert (await client.get("/v1/routing")).status_code == 404
        assert (await client.get("/v1/metrics")).status_code == 404


@pytest.mark.asyncio
async def test_admin_endpoints_enabled(make_client, sample_config, tmp_path) -> None:
    _write_prompt_assets(tmp_path)
    config = sample_config.model_copy()
    config.admin_endpoints.enabled = True
    async with make_client(config=config, prompts_dir=tmp_path) as client:
        ok = await client.post(
            "/v1/llm",
            json={
                "model": "chat-fast",
                "messages": [{"role": "user", "content": "s"}],
                "prompt": {
                    "name": "knowledge_decision",
                    "version": "v1",
                    "variables": {"product_name": "助手"},
                },
            },
        )
        assert ok.status_code == 200, ok.text
        traces = await client.get("/v1/traces")
        assert traces.status_code == 200
        trace_items = traces.json()
        assert any(item["prompt_name"] == "knowledge_decision" for item in trace_items)
        assert all("content" not in item for item in trace_items)
        metrics = await client.get("/v1/metrics")
        assert metrics.status_code == 200
        assert metrics.json()["requests_total"] >= 1
        routing = await client.get("/v1/routing")
        assert routing.status_code == 200
        assert routing.json()
