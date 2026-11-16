# llm-gateway

从 `week01/1-6/gateway.py` 单文件网关重构而来的分层 LLM 网关：统一非流式调用、SSE 流式输出、Structured Output、能力路由 + 主备降级、Prompt 模板资产与调用审计。

## 架构

```
api（端点/协议翻译） → service（业务编排） → core（基础设施抽象）
```

依赖方向单向：core 不 import service/api，service 不 import api，装配只发生在 `app/app.py`。

| 层 | 职责 | 关键模块 |
|---|---|---|
| api | 端点、OpenAI facade 翻译、流式格式化、错误映射 | `api/routes.py` / `api/schemas_openai.py` / `api/streaming.py` / `api/errors.py` |
| service | 路由计划执行、重试/降级、L4 校验、流终态状态机、审计触发 | `service/llm_service.py` / `service/retry.py` |
| core | 领域模型、配置、路由决策、健康/容量状态机、供应商适配、Prompt 引擎、校验修复、审计、Metrics、日志 | `core/` |

## 快速开始

```bash
pip install -r requirements.txt

# 环境变量（API Key 只从环境变量读取，配置中只存变量名）
$env:DEEPSEEK_API_KEY = "sk-..."
$env:DEEPSEEK_BACKUP_API_KEY = "sk-..."

uvicorn app.app:create_app --factory --port 8000
```

启动参数（`config.yaml` 全部可配置）：模型白名单/能力/优先级/价格、健康熔断、并发限流、重试退避、Prompt 资产、管理面开关、日志。

## 端点

| 端点 | 说明 |
|---|---|
| `POST /v1/llm` | 网关原生统一端点；`stream=true` 返回 SSE；支持 `prompt`/`response_schema`/`trace_id` |
| `POST /v1/llm/stream` | 流式便捷别名（等价 `stream=true`） |
| `POST /v1/chat/completions` | OpenAI 兼容 facade，SDK 无缝接入；流式返回 `chat.completion.chunk` + `[DONE]` |
| `GET /v1/traces` | 调用审计（受 `admin_endpoints.enabled` 开关控制，默认关闭） |
| `GET /v1/routing` | 路由决策留痕（管理面） |
| `GET /v1/metrics` | Metrics 快照（管理面） |

所有响应通过 `X-Request-Id`（call_id）/ `X-Trace-Id` 响应头返回两级关联 ID。

### 网关原生请求

```json
{
  "model": "chat-fast",
  "messages": [{"role": "user", "content": "你好"}],
  "response_schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
  "prompt": {"name": "knowledge_decision", "version": "v1", "variables": {"product_name": "差旅助手"}},
  "timeout_seconds": 30,
  "trace_id": "optional-client-trace-id"
}
```

- `model` 缺省走纯能力路由；显式指定时硬校验（`unknown_model` 400，能力不满足 400），绝不静默换模型
- `timeout_seconds` 是全链路总预算（跨重试与降级共享），剩余不足 → `insufficient_time_budget`(429)

### OpenAI 兼容

`temperature/top_p/n/stop/seed/user` 等暂不支持字段接受后忽略；`response_format`（`json_object`/`json_schema`）在边界翻译为 `response_schema`，响应 `model` 字段为平台逻辑模型名。

## 错误模型

| 层 | 场景 | 错误码 | HTTP |
|---|---|---|---|
| 请求校验 | 模型不在白名单 / 能力不满足 | `unknown_model` / `structured_output_unsupported` | 400 |
| Prompt | 缺变量 / 超长 / 超预算 | `missing_prompt_variable` / `prompt_variable_too_long` / `context_budget_exceeded` | 400 |
| 路由 | 无兼容模型 / 预算不足 | `no_compatible_model`(503) / `insufficient_time_budget`(429) | — |
| L1/L2 | 连接/超时/限流/服务端错误（有限重试 + 换模型降级） | `provider_*`，全败 → `model_unavailable` | 502 |
| L4 | JSON 解析 / Schema 校验失败（本地有界修复，不重试不降级） | `invalid_json` / `schema_validation_failed` | 502 |

响应体统一 `{"detail": {"code": "...", "message": "..."}}`。

## 测试

```bash
python -m pytest -q        # 129 个用例：适配器契约/路由/重试/流式/结构化输出/可观测/编排/API
```

分阶段测试对应设计文档 §12；供应商适配通过 `httpx.MockTransport` 契约测试，服务层注入 `FakeProvider`，零真实网络依赖（真实 DeepSeek 集成测试见基线 `test_gateway2.py` 思路，按需自备 Key 验证）。

## 与基线的破坏性变更

- 响应体：平铺字段 → 嵌套结构（`latency`/`resilience`/`cost`），`request_id` → `call_id`
- `timeout_seconds`：每次上游 SDK 超时 → 全链路总预算
- Prompt 资产：移除 `default_model`/`output_schema`/`generation_params` 字段（模板只做渲染，不绑模型）
- 审计不保存 Prompt 正文与模型回答内容（白名单原则）
- `responses`/`tool_calling` 协议本期不做（YAGNI），仅 `chat_completion` 上游协议

详见 `docs/superpowers/specs/2026-11-15-llm-gateway-layered-architecture-design.md`。
