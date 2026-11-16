# LLM Gateway 分层架构重构设计文档（v2）

- **日期**: 2026-11-16
- **状态**: 已按代码审查意见修订（v2）
- **基线代码**: `d:/develop/agent_study/week01/1-6/gateway.py`（439 行，FastAPI + OpenAI SDK）
- **目标目录**: `d:/develop/agent_study/week01/llm-gateway/`
- **范围**: 将单文件 gateway.py 重构为 api/service/core 三层架构，并引入配置驱动、能力路由、Prompt 管理、可观测性与分阶段测试
- **定位声明**: 本目录是 1-6 代码的**重构 + 扩展**版本：保留原有优秀设计（Provider 协议隔离、流式首块边界、白名单审计、错误码标准化），修复基线缺陷（连接池、全局可变状态、硬编码备用模型名、流式审计缺 usage、成本 KeyError），并引入有明确收益的扩展（能力路由、OpenAI facade、容量守卫）。**不承诺向后兼容**：当破坏性变更更符合工程最佳实践时直接采纳，所有破坏性变更统一在第 13 节声明。

## 1. 背景与目标

现有 `gateway.py` 是单文件 LLM 网关：统一非流式调用、SSE 流式输出、Structured Output、主备模型 fallback、Prompt 模板与调用审计。目标：

1. 分层架构：api（路由与协议翻译）/ service（业务编排）/ core（基础设施抽象），依赖方向单向：`api → service → core`
2. 配置驱动：模型、供应商、降级、重试、限流、日志全部由 `config.yaml` 声明；API Key 仅从环境变量读取
3. 能力路由：按能力匹配 + 优先级 + 成本 + 健康状态选择模型，取代静态主备 fallback
4. 可观测：调用关联 ID、分阶段延迟、分项成本、路由决策留痕（可复盘）
5. 修复基线已知缺陷：连接池复用、全局可变状态、硬编码备用模型名、流式审计缺 usage/TTFT、成本 KeyError

## 2. 目录结构

```
week01/llm-gateway/
├── config.yaml                     # 模型/能力/优先级/价格/重试/限流/管理面开关/日志
├── requirements.txt                # fastapi/uvicorn/openai/pydantic/jsonschema/httpx + pyyaml + jinja2
├── README.md
├── prompts/                        # Prompt 发布资产（YAML 文件）
│   └── knowledge_decision/
│       └── v1.yaml
├── app/
│   ├── __init__.py
│   ├── app.py                      # FastAPI 实例、路由挂载（含管理面开关）、关联中间件、异常处理器、DI 装配、lifespan 关闭
│   ├── api/
│   │   ├── __init__.py
│   │   ├── schemas_openai.py       # OpenAI 兼容 facade Schema（请求/响应/chunk）+ to_internal/to_openai_*
│   │   ├── routes.py               # 端点（6 个，其中 3 个受管理面开关控制）
│   │   ├── streaming.py            # StreamEvent → chat.completion.chunk / 网关 SSE 两种格式化
│   │   └── errors.py               # GatewayError → HTTPException 统一映射
│   ├── service/
│   │   ├── __init__.py
│   │   ├── llm_service.py          # complete/stream/list_traces：路由计划执行、四层检查、L4 校验、审计、流终态状态机
│   │   └── retry.py                # RetryPolicy、is_retryable_error、backoff_delay（纯函数）
│   ├── core/
│   │   ├── __init__.py
│   │   ├── schemas.py              # Message/Usage/LLMRequest/LLMResponse/CallTrace/StreamEvent/关联模型
│   │   ├── errors.py               # GatewayError + 稳定错误码常量
│   │   ├── config.py               # AppConfig/ModelConfig/RetryConfig/LimitsConfig/AdminConfig/LoggingConfig + YAML/env 加载 + 启动校验
│   │   ├── capabilities.py         # Capability 枚举（streaming/structured_output）
│   │   ├── router.py               # ModelRouter 纯决策：RouteRequest/CandidatePlan/plan() + 显式模型硬校验
│   │   ├── health.py               # HealthTracker：连续失败熔断 + 冷却恢复
│   │   ├── limits.py               # CapacityGuard：全局并发 + 每模型池 + 剩余预算检查
│   │   ├── metrics.py              # calculate_cost（分项）+ MetricsRecorder/InMemoryMetricsRecorder
│   │   ├── prompts.py              # PromptPackage/PromptVariable + PromptRegistry + SafePromptRenderer + PromptEngine
│   │   ├── validation.py           # SchemaValidator 协议 + Pydantic/Jsonschema 实现 + IncrementalJsonAccumulator + RepairPolicy
│   │   ├── audit.py                # AuditStore/InMemoryAuditStore + RoutingDecisionLog
│   │   ├── logging.py              # RequestIdFilter/SensitiveDataFilter/JsonFormatter/setup_logging
│   │   └── providers/
│   │       ├── __init__.py         # register_provider/get_provider 注册表
│   │       ├── base.py             # Provider 协议/ProviderCall/ProviderResult/分层异常层级
│   │       └── openai_compatible.py# chat_completion 上游协议适配 + SDK 异常翻译 + 客户端缓存 + client_factory 注入
└── tests/
    ├── conftest.py                 # app/config fixtures、ASGI client、MockTransport 构造器、fake_sleep
    ├── test_adapter_contract.py    # ① Adapter Contract
    ├── test_router.py              # ② Router（含显式模型语义）
    ├── test_retry.py               # ③ 重试（不真等待）
    ├── test_streaming.py           # ④ Streaming 事件序列
    ├── test_structured_output.py   # ⑤ Structured Output
    ├── test_observability.py       # ⑥ Trace/Metrics/Cost
    ├── test_api.py                 # 网关原生端点（旧 test_gateway.py 用例按新契约迁移）
    ├── test_api_openai_compat.py   # OpenAI facade
    ├── test_service.py             # 编排集成
    ├── test_core_config.py         # YAML/env/校验/管理面开关
    └── test_core_audit_logging.py  # 审计白名单/脱敏/关联 ID
```

## 3. 配置设计（config.yaml）

```yaml
version: "1.0"                       # 配置版本号，供路由决策留痕对齐

models:
  chat-fast:                         # 平台逻辑模型名（业务语义命名，不再使用主备命名）
    adapter: openai_compatible
    capabilities: [streaming, structured_output]   # 须为 adapter 能力子集
    priority: 100                    # 业务优先级（路由排序）
    provider_model: deepseek-v4-flash
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY    # 只存 env 变量名，不存密钥
    structured_output_mode: json_object
    pricing: {input: 1.0, cached_input: 0.1, output: 4.0}
  chat-pro:
    adapter: openai_compatible
    capabilities: [streaming, structured_output]
    priority: 80
    provider_model: deepseek-v4-pro
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_BACKUP_API_KEY
    structured_output_mode: json_schema
    pricing: {input: 0.8, cached_input: 0.08, output: 3.2}

routing:
  health: {consecutive_failure_threshold: 3, cooldown_seconds: 30}

limits:
  max_concurrent_requests: 100
  per_model_max_concurrent: 20
  queue_timeout_seconds: 5
  min_remaining_budget_seconds: 1.0

retry:
  max_attempts_per_model: 2
  base_delay_seconds: 0.1
  backoff_factor: 2.0
  max_delay_seconds: 2.0             # 退避封顶
  jitter: true

prompts:
  chars_per_token_estimate: 2.0      # Context 预算的 token 估算系数（中文保守值，见 8.3）

admin_endpoints:
  enabled: false                     # /v1/traces、/v1/routing、/v1/metrics 默认不挂载（见 10.3）

logging:
  level: INFO
  access: {handler: console, format: json}
  error: {handler: console, format: json}
  audit: {handler: console, format: json}
```

- 加载规则：YAML 为唯一配置来源；环境变量仅用于 `api_key_env` 指向的 API Key；不再读取 `PRIMARY_PROVIDER_MODEL` / `PRIMARY_BASE_URL` / `BACKUP_PROVIDER_MODEL` / `BACKUP_BASE_URL` 旧环境变量（破坏性变更，见 13.2）
- 启动校验：能力为 adapter 能力子集、priority 与 pricing 必填、api_key_env 非空 → 启动即报错（消除基线"成本表 KeyError 在调用成功后才抛 500"的缺陷）

## 4. 分层职责与依赖方向

```
api（路由 + 协议翻译）→ service（编排）→ core（基础设施抽象）
core 不 import service/api；service 不 import api；装配只在 app.py
```

| 层 | 职责 | 关键模块 |
|---|---|---|
| api | 端点、外部协议 ↔ 内部模型翻译、流式格式化、错误映射 | schemas_openai.py / routes.py / streaming.py / errors.py |
| service | 路由计划执行、重试/降级、四层检查、L4 校验、流终态状态机、审计触发 | llm_service.py / retry.py |
| core | 领域模型、配置、能力、路由纯决策、健康/容量状态机、供应商适配、Prompt 引擎、校验/修复、审计存储、Metrics、日志 | schemas/errors/config/capabilities/router/health/limits/metrics/prompts/validation/audit/logging/providers |

### 4.1 api 层

端点清单：

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI 兼容 facade：OpenAI Chat Completions Schema 进出，SDK 无缝接入；流式返回 `chat.completion.chunk` + `[DONE]` |
| `POST /v1/llm` | 网关原生统一端点；stream=true 返回 SSE；支持 prompt/response_schema/trace_id 扩展 |
| `POST /v1/llm/stream` | 流式便捷别名（等价 stream=true） |
| `GET /v1/traces` | 调用审计（管理面，受 admin_endpoints 开关控制） |
| `GET /v1/routing` | 路由决策记录（归因/复盘，管理面） |
| `GET /v1/metrics` | Metrics JSON 快照（管理面） |

网关原生请求模型（本期仅 `chat_completion` 上游协议，`responses` 协议见 YAGNI 清单，故不引入协议判别联合）：

```python
# core/schemas.py —— 网关原生协议，单一请求模型
class LLMRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None            # 可选：缺省走纯能力路由（见第 7 节显式模型语义）
    messages: list[Message] = Field(min_length=1, max_length=100)
    stream: bool = False
    response_schema: dict[str, Any] | None = None
    prompt: PromptSelection | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=120)   # 全链路总预算（见第 6 节）
    trace_id: str | None = None

    @model_validator(mode="after")
    def check_supported_combination(self) -> "LLMRequest":
        if self.stream and self.response_schema is not None:
            raise ValueError("stream 与 response_schema 不能同时使用")
        return self
```

> 保留 `response_schema` 字段名（与基线一致）：网关原生协议本就用 JSON Schema 语义，改名只会徒增一层翻译；OpenAI 命名对齐（`response_format`）交由 facade 层在边界完成，二者职责分离。

OpenAI facade（`schemas_openai.py`）：OpenAI Chat Completions 形状的请求/响应/chunk 模型；`temperature/top_p/n/stop/seed/user` 等网关暂不支持的字段**接受后忽略 + debug 日志**（保证任意 OpenAI SDK 调用不炸）；`to_internal()` 在边界完成协议分离（`response_format → response_schema`）；`to_openai_response()` 反向翻译：

- 响应 `model` 字段 = **resolved 平台逻辑模型名**（绝不暴露 provider_model/base_url/密钥）
- 关联 ID 通过响应头返回：`X-Request-Id`（call_id）、`X-Trace-Id`
- 流式失败：未发首块 → HTTP 错误；已发首块 → 断流（SDK 侧表现为异常）；网关 SSE 侧显式 `response.failed`

### 4.2 service 层

```python
class LLMService:
    def __init__(self, config: AppConfig, router: ModelRouter, health: HealthTracker,
                 capacity: CapacityGuard, prompts: PromptEngine, audit: AuditStore,
                 metrics: MetricsRecorder, sleep=asyncio.sleep): ...
    async def complete(self, request: LLMRequest) -> LLMResponse: ...
    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamEvent]: ...
    def list_traces(self) -> list[CallTrace]: ...
    def list_routing_decisions(self) -> list[RoutingDecision]: ...
```

执行链路（complete）：

```
LLMRequest → 关联 ID 落定 → 推导所需能力 → PromptEngine（校验/渲染/预算）
→ 显式模型硬校验（unknown_model 400 / structured_output_unsupported 400，见第 7 节）
→ ModelRouter.plan(request, config, health) → 逐候选执行：
    CapacityGuard.acquire（模型池满 → 记录 capacity_full → 下一候选；全局满 → 429）
    → 剩余预算检查（不足 → insufficient_time_budget 429）
    → provider.complete（单次上游超时 = 剩余预算，RetryPolicy 内重试）
    → L3 协议检查 → L4 校验（含本地有界修复） → 成功即返回
    候选失败 → reject_reason 记录 → 下一候选
→ 全程更新 CallTrace + RoutingDecision + Metrics
```

### 4.3 core 层关键签名

```python
# errors.py
class GatewayError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502): ...

# schemas.py
class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0

class LatencyBreakdown(BaseModel):
    queue_ms: int = 0
    route_ms: int = 0
    ttft_ms: int | None = None
    generation_ms: int = 0
    total_ms: int = 0

class ResilienceInfo(BaseModel):
    attempts: int = 0
    retries: int = 0
    fallbacks: int = 0
    timeout_budget_seconds: float | None = None
    timeout_consumed_seconds: float = 0

class CostBreakdown(BaseModel):
    input_usd: float = 0
    cached_usd: float = 0
    output_usd: float = 0
    total_usd: float = 0

# 判别联合：合法 Python 类型语法
StreamEvent = Annotated[
    StreamDelta | StreamUsage | StreamCompleted | StreamFailed | StreamCancelled,
    Field(discriminator="type"),
]
```

CallTrace 全字段（见第 10 节）。

## 5. 供应商暴露策略（Provider 协议 + 注册表 + 能力 + 客户端生命周期）

```python
# core/capabilities.py
class Capability(StrEnum):
    STREAMING = "streaming"
    STRUCTURED_OUTPUT = "structured_output"

# core/providers/base.py
class Provider(Protocol):
    capabilities: ClassVar[set[Capability]]

    async def complete(self, config: ModelConfig, call: ProviderCall) -> ProviderResult: ...
    async def stream(self, config: ModelConfig, call: ProviderCall) -> AsyncIterator[str]: ...
    async def close(self) -> None: ...          # 释放连接池，app lifespan 关闭时调用

@dataclass(frozen=True)
class ProviderCall:
    messages: list[Message]
    timeout_seconds: float                       # 本次上游 attempt 超时（= 服务层分配的剩余预算）
    response_schema: dict[str, Any] | None

@dataclass(frozen=True)
class ProviderResult:
    content: str
    usage: Usage
    finish_reason: str | None
    provider_request_id: str | None

# core/providers/__init__.py —— 注册表
_PROVIDER_REGISTRY: dict[str, type[Provider]] = {}
def register_provider(name: str, cls: type[Provider]) -> None: ...
def get_provider(name: str) -> Provider: ...     # 未知 → GatewayError("unknown_provider_adapter", 503)

register_provider("openai_compatible", OpenAICompatibleProvider)
```

客户端生命周期（修复基线"每次调用新建客户端、丢失 httpx 连接池复用"缺陷）：

```python
class OpenAICompatibleProvider:
    def __init__(self, client_factory: Callable[[ModelConfig], AsyncOpenAI] | None = None) -> None:
        self._clients: dict[tuple[str, str], AsyncOpenAI] = {}   # (base_url, api_key) 键控

    def get_client(self, config: ModelConfig) -> AsyncOpenAI:
        # 惰性创建 + 按 (base_url, api_key) 缓存复用 → 同一上游的多次调用共享连接池
        # api_key 缺失 → GatewayError("gateway_misconfigured", 503)
        # 单事件循环内 dict 缓存无锁安全（asyncio 单线程模型）

    async def close(self) -> None:
        # 遍历 await client.close()，由 app lifespan 在进程退出时调用
```

- service 只经 `get_provider(model.adapter)` 拿协议对象，对供应商零感知
- 模型能力 = adapter 能力 ∩ 模型声明能力，加载时校验子集关系
- 可测性接缝：`client_factory` 注入 MockTransport（与客户端缓存正交：缓存键不变，工厂仅决定底层 transport）

## 6. 错误模型（四层检查点 + 阶段策略矩阵 + 重试）

### 6.1 四层检查点

| 层 | 失败场景 | 检测点 | 异常（错误码） | retryable | fallback_allowed |
|---|---|---|---|---|---|
| L1 网络 | DNS/连接/超时 | 适配器捕获 SDK 异常 | `ProviderConnectionError(provider_connection_error)`、`ProviderTimeoutError(provider_timeout)` | ✅ | ✅ |
| L2 HTTP | 429 | `RateLimitError`（尊重 Retry-After） | `ProviderRateLimitError(provider_rate_limited, retry_after_seconds)` | ✅ | ✅ |
| L2 HTTP | 5xx | `APIStatusError` 500-599 | `ProviderServerError(provider_server_error)` | ✅ | ✅ |
| L2 HTTP | 401/403 | `APIStatusError` | `ProviderAuthError(provider_auth_error, 503)` | ❌ | ❌ |
| L2 HTTP | 其余 4xx | `APIStatusError` | `ProviderBadRequestError(provider_bad_request)` | ❌ | ❌ |
| L3 协议 | choices 空/结构缺失 | 适配器响应后校验 | `ProviderInvalidResponseError(provider_invalid_response)` | ❌ | ✅ |
| L4 schema | JSON 解析/Schema 校验失败 | service 校验阶段 | `GatewayError(invalid_json / schema_validation_failed, 502)` | ❌ | ❌ |

### 6.2 阶段错误策略矩阵（全阶段）

| 阶段 | 典型错误 | 错误码 | 重试 | 换模型 fallback | 修复策略 | 说明 |
|---|---|---|---|---|---|---|
| 认证 | 上游 401/403、凭据缺失 | provider_auth_error / gateway_misconfigured | ❌ | ❌ | — | 网关侧租户鉴权本期不做（YAGNI） |
| 请求校验 | 参数非法、非法组合 | FastAPI 422 / unknown_model / structured_output_unsupported / unsupported_combination | ❌ | ❌ | — | |
| Prompt | 缺变量/超长/超预算 | missing_prompt_variable / prompt_variable_too_long / context_budget_exceeded（400） | ❌ | ❌ | — | 调用方修正请求后再试 |
| 路由 | 无兼容模型 | no_compatible_model（503） | ❌ | ❌ | — | 配置变化后再试 |
| 排队 | 并发满/预算不足 | too_many_requests（429）/ insufficient_time_budget（429） | ❌ | 换池 | — | 全局满→429；模型池满→记录 capacity_full 走下一候选 |
| 连接 | 网络抖动、短暂 5xx | provider_connection_error / provider_timeout / provider_server_error | ✅ 有限 | ✅ | — | |
| 首 Token 前 | 429/超时/过载 | provider_rate_limited 等 | ✅ 有限 | ✅ | — | |
| 已流式输出 | 连接中断 | （cancelled 终态） | ❌ | ❌ | — | 不盲目重新生成、不拼接 |
| 输出校验 | JSON/Schema 错误 | invalid_json / schema_validation_failed | ❌ 上游不重试 | ❌ | 本地有界修复 ≤ max_strategies | 修复仅为本地文本后处理，绝不触发上游重试 |
| 内容拒答 | 安全策略拒绝 | finish_reason=content_filter | ❌ | ❌ | — | 不换模型绕过；作为正常终态返回 |

> 术语约定：本表"换模型 fallback"指切换候选模型；"修复策略"指 L4 输出校验阶段的本地文本修复。二者不再混用同一个词。

### 6.3 重试与超时语义

```python
# service/retry.py
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts_per_model: int = 2
    base_delay_seconds: float = 0.1
    backoff_factor: float = 2.0
    max_delay_seconds: float = 2.0
    jitter: bool = True

def is_retryable_error(exc: Exception) -> bool: ...     # 查 ProviderError.retryable 属性
def allow_fallback(exc: Exception) -> bool: ...         # 查 ProviderError.fallback_allowed 属性
def backoff_delay(attempt: int, policy: RetryPolicy) -> float: ...
    # delay = min(base * factor ** (attempt - 1), max)，jitter=true 时乘以 [0.5, 1.5] 随机系数
    # 纯函数，可测；LLMService 注入 sleep（默认 asyncio.sleep），测试注入 fake_sleep，零真实等待
```

- 有边界指数退避：上限 `max_delay_seconds`，jitter 消峰
- **timeout_seconds 语义变更（声明）**：基线的 `timeout_seconds` 是每次上游调用的 SDK 超时（重试/fallback 每次都可用满）；新设计改为**全链路总预算**——请求开始到响应完成的总时限，跨重试与 fallback 共享。每次上游 attempt 的超时 = 剩余预算；剩余 < `min_remaining_budget_seconds` 时不再尝试，返回 `insufficient_time_budget`(429)
- **流式重试边界**：首 delta 前可重试/fallback；之后不可（终态唯一）
- **幂等性边界**：网关不提供计费幂等保证——每次上游 attempt 都可能真实计费；CallTrace 记录 `provider_request_ids`（每次尝试的上游 ID）供对账

## 7. 路由设计（能力 + 优先级 + 成本 + 健康）

```python
# core/router.py —— 纯函数，输入全部可序列化（决策可重建）
@dataclass(frozen=True)
class RouteRequest:
    requested_model: str | None
    required_capabilities: set[Capability]

@dataclass(frozen=True)
class CandidatePlan:
    model: str
    order: int
    priority: int
    cost_per_1m_tokens: float
    reject_reason: str | None

class ModelRouter:
    def plan(self, request: RouteRequest, config: AppConfig,
             health: HealthTracker) -> list[CandidatePlan]:
        # 1) 能力过滤（required ⊆ capabilities，不满足 → reject_reason=capability_missing:*）
        # 2) 健康过滤（unhealthy → reject_reason=unhealthy:*）
        # 3) 排序：priority 降序 → cost 升序 → 声明顺序（完全确定）
        # 4) requested_model 指定且兼容时排首位，其余候选仍按规则排序跟随
```

**显式模型硬校验语义（service 在 plan() 之前执行，修复基线"能力不足静默换模型"的隐患）**：

- `model` 显式指定：
  - 模型不存在 → `unknown_model`(400)；
  - 模型存在但能力不满足 → `structured_output_unsupported`(400)；
  - 以上两种情况**绝不静默切换到其他模型**。
  - 模型兼容但**临时不可用**（健康熔断 / 池满 / 上游临时故障）→ 允许降级到能力等价候选（继承基线主备切换的容错语义，属于弹性行为而非能力欺骗）。
- `model=None` → 纯能力路由：候选 = 能力满足 ∩ 健康，按排序规则取第一个；候选为空 → `no_compatible_model`(503)。

健康状态机（core/health.py）：

```python
class HealthTracker:
    def record_success(self, model: str) -> None: ...
    def record_failure(self, model: str) -> None: ...
    def is_healthy(self, model: str) -> bool: ...   # 连续失败 ≥ 阈值 → 熔断；冷却期后恢复
```

路由决策留痕（用于故障归因与路由策略复盘；`plan()` 纯函数保证同输入同输出，决策可重建）：

```python
class RoutingDecision(BaseModel):
    call_id: str
    timestamp: datetime
    config_version: str
    required_capabilities: list[str]
    health_snapshot: dict[str, str]
    candidates: list[CandidateDecision]   # 含被拒者与最终选中者

class CandidateDecision(BaseModel):
    model: str
    order: int | None
    reject_reason: str | None
    selected: bool
    attempts: int
    final_error_code: str | None
```

## 8. Prompt 管理（可发布版本 + 严格渲染）

### 8.1 PromptPackage（10 个字段，按审查意见剔除本期不实现的预留项）

```python
class PromptPackage(BaseModel):
    name: str                              # ① 模板名
    version: str                           # ② 版本
    status: Literal["draft", "published", "deprecated"]   # ③ 发布状态
    system_template: str                   # ④ System Prompt 模板
    variables: dict[str, PromptVariable]   # ⑤ 输入变量 Schema
    default_model: str | None = None       # ⑥ 默认逻辑模型
    output_schema: dict[str, Any] | None = None   # ⑦ 默认输出 JSON Schema（缺省 response_schema）
    generation_params: dict[str, Any] = {} # ⑧ 默认生成参数
    context_budget: int | None = None      # ⑨ Context 预算（估算 token）
    prompt_hash: str                       # ⑩ sha256(渲染后 system 序列化)
```

- 存放：`prompts/<name>/<version>.yaml` 发布资产；PromptRegistry 加载缓存
- `status=draft/deprecated` 默认拒绝调用（配置可放开）
- few_shots / tool_version / eval_gate / changelog 等元数据本期**不实现**（YAGNI，见第 14 节）

### 8.2 严格渲染引擎

- jinja2 `SandboxedEnvironment` + `StrictUndefined`（沙箱，无 eval/exec）
- 渲染前 AST 白名单校验：仅允许变量替换 `{{var}}` 与条件分支 `{% if %}...{% endif %}`；For/Macro/Import/属性访问/函数调用一律拒绝
- 变量校验（渲染前，不进上游）：未知变量 `unknown_prompt_variable`(400)、缺失 `missing_prompt_variable`(400)、类型不符 `invalid_prompt_variable`(400)、超长 `prompt_variable_too_long`(400)、含换行/控制字符 `invalid_prompt_variable`(400)
- **威胁模型声明**：调用方为受信内部 Agent（网关无租户鉴权，见 YAGNI）。沙箱 + AST 白名单防止模板侧代码执行；变量值校验（长度/类型/禁控制字符）收窄注入面。语义级 Prompt 注入防御（识别"忽略以上规则"类指令）不在本期范围。

### 8.3 运行时语义

```
请求.prompt = {name, version, variables}
→ 取包（status 检查）→ 变量 Schema 校验 → 沙箱渲染
→ 默认值补位：model ← default_model；response_schema ← output_schema
→ generation_params 合并 → context_budget 检查 → 上游调用
→ CallTrace 记录 prompt_name/version/hash
```

- Context 预算估算：`tokens_est = ceil(chars / chars_per_token_estimate)`，默认系数 2.0（中文保守值：1 汉字 ≈ 1~1.5 token；基线的 chars/4 启发式对中文低估 3~6 倍，已修正）；纯英文场景可在配置调为 4.0。超限 → `context_budget_exceeded`(400)

## 9. 流式设计

### 9.1 内部事件序列（统一）

`StreamDelta → StreamUsage → 终态（StreamCompleted | StreamFailed | StreamCancelled）`

对外映射：

- 网关 SSE：`content.delta` / `response.completed`（含 usage/parsed）/ `response.failed`；cancelled 不 emit（连接已断，只写审计）
- OpenAI facade：`chat.completion.chunk`（delta.content）+ 末块（usage + finish_reason:"stop"）+ `[DONE]`

### 9.2 终态唯一性

- 状态机：COMPLETED / FAILED / CANCELLED 互斥；emit 任一终态后立即 return，结构上杜绝中途切换终态类型
- 取消：捕获 `asyncio.CancelledError`（客户端断开）→ 审计 `status=cancelled`，不 emit 事件
- Fallback 边界：首 delta 发出前可 fallback；发出后只允许结束流，绝不换模型拼接

### 9.3 TTFT 定义

**TTFT = 请求开始 → 第一个有业务意义的 delta 发出的耗时**。业务意义 delta = 非空内容增量；连接建立/空 delta 不计。适配器保证只 yield 非空业务 delta，service 在首个 StreamDelta 产出时打点。

### 9.4 Structured Streaming 边界（状态机实现，修正 raw_decode 直接尝试的误判缺陷）

```python
class IncrementalJsonAccumulator:
    # 内部维护小型状态机：depth（{} [] 嵌套深度）、in_string、escape 三态
    def feed(self, chunk: str) -> None:
        # 追加缓冲并更新状态机；
        # 仅当 depth 回到 0（JSON 结构已闭合）时才尝试 json.JSONDecoder.raw_decode：
        #   成功 → 记为完整 JSON 片段（支持流式多片段场景）；
        #   失败 → "结构已闭合仍不可解析" = 硬性畸形 → 提前终止流（response.failed: invalid_json）
        # 结构未闭合时不尝试解析——合法流式片段必然处于未闭合态，避免正常流被中途误判
    def final(self) -> Any:
        # 要求 depth == 0 且解析成功；Schema 校验只在流结束后做一次：
        # 通过 → response.completed(parsed)；失败 → response.failed
```

- 流中部分 JSON 不做 Schema 校验（无意义）
- 早失败目的：不让调用方收完坏流才报错；判定依据是"闭合 + 解析失败"，而非任何单次 JSONDecodeError

## 10. 可观测性设计

### 10.1 两级调用关联

| ID | 语义 | 来源 |
|---|---|---|
| trace_id | 根链路（任务级） | 请求头 `X-Trace-Id` / 网关原生 body 字段；缺失自动生成 |
| call_id | 一次模型调用 | 始终新生成（= 基线 request_id 语义） |

ContextVar 贯穿全链路；响应头回显 `X-Trace-Id` / `X-Request-Id`（call_id）供调用方传播到下一跳。run_id/step_id 两级关联本期不实现（当前无多级编排场景，YAGNI 预留扩展位）。

### 10.2 CallTrace 全字段

```python
class CallTrace(BaseModel):
    # 关联
    trace_id: str; call_id: str
    # 路由
    requested_model: str | None; actual_model: str | None
    provider: str | None                    # adapter 名
    routing_decision_id: str | None
    # Prompt
    prompt_name: str | None; prompt_version: str | None; prompt_hash: str | None
    # 用量 / 延迟 / 弹性 / 成本
    usage: Usage
    latency: LatencyBreakdown
    resilience: ResilienceInfo
    cost: CostBreakdown
    # 结果与错误
    finish_reason: str | None
    status: Literal["success", "failed", "cancelled"]
    validation_result: Literal["passed", "failed", "not_applicable"]
    error_code: str | None
    http_status: int | None
    provider_request_ids: list[str]         # 每次上游尝试的 ID（计费对账）
```

- 白名单原则：不存 Prompt 正文与模型回答内容
- 脱敏：代码层不传敏感值给 logger（主防线）→ 存储层 schema 即白名单 → 展示层 `SensitiveDataFilter` 掩码兜底（`sk-*`/Bearer/截断 200 字符）
- 说明：CallTrace 结构相对基线为破坏性变更（平铺字段 → 嵌套结构），见 13.2

### 10.3 管理面端点与暴露面收窄

- `/v1/traces`、`/v1/routing`、`/v1/metrics` 为管理面端点，由 `admin_endpoints.enabled` 控制挂载，**默认 false（不挂载，返回 404）**
- 启用场景：部署于内网的可信环境；网关侧租户鉴权不在本期（YAGNI），以"默认关闭"降低未鉴权暴露面（路由决策含健康快照/拒绝原因等内部情报）

### 10.4 Metrics / Logs / Trace 分工

| 类型 | 载体 | 内容 |
|---|---|---|
| Metrics（聚合趋势） | InMemoryMetricsRecorder → `GET /v1/metrics`（JSON 快照，实现可替换为 Prometheus） | QPS、P50/P95/P99（total/TTFT）、错误率、错误码计数、Token 吞吐 |
| Logs（单次细节） | access / error / audit 三个命名 logger（JsonFormatter，`propagate=False` 隔离） | 访问（不含 body）、错误（含 call_id/error_code，不含内容）、审计（JSON 单行） |
| Trace（链路串联） | CallTrace + RoutingDecision | 两级 ID 串联多次调用，单次调用全维度落库 |

- 关联 ID 由中间件注入 ContextVar，`RequestIdFilter` 自动附加到每条日志
- Metrics 由 CallTrace 派生，不重复采集

### 10.5 成本分项

`cost_input = input × input价`、`cost_cached = cached × cached_input价`、`cost_output = output × output价`、`total = 三者之和`（价格取自模型配置，启动校验保证完整性）。

## 11. Structured Output 两层保证 + 有界修复

- **第一层**（请求时带 Schema）：`json_object`（Schema 注入 system）/ `json_schema`（原生 strict）
- **第二层**（本地校验，应用层安全）：不信任供应商承诺，本地 `SchemaValidator` 校验

```python
# core/validation.py
class SchemaValidator(Protocol):
    def validate(self, instance: Any, schema: dict[str, Any]) -> Any: ...

class PydanticSchemaValidator:      # 默认：JSON Schema → 动态模型 → TypeAdapter.validate
class JsonschemaFallbackValidator:  # 兜底：Pydantic 不支持的关键字

@dataclass(frozen=True)
class RepairPolicy:
    max_strategies: int = 3          # 有界，可配置

REPAIR_STRATEGIES = [direct_parse, strip_markdown_fence, extract_first_balanced_object]
# 逐策略尝试（仅本地文本后处理，绝不触发上游重试）；全败 → invalid_json 显式报错
# 修复上限 = max_strategies（本地策略数）。L4 失败不触发上游重试，attempts 恒为 1，
# 因此上限与 max_attempts_per_model 无关（修正 v1 文档的公式错误）
```

## 12. 测试策略（分阶段）

| 阶段 | 文件 | 要点 |
|---|---|---|
| ① Adapter Contract | test_adapter_contract.py | MockTransport 驱动：请求翻译（method/path/body）、响应翻译（content/usage/finish_reason/provider_request_id）、异常翻译、流式 chunk→delta、**客户端复用**（同 (base_url, key) 两次调用共享底层连接）与 close() 释放、**客户端取消**（ASGI 层断开 → 干净退出 + status=cancelled） |
| ② Router | test_router.py | 能力匹配、优先级/成本/声明序、健康过滤、拒绝原因逐条断言、**显式模型语义**（unknown_model 400 / structured_output_unsupported 400 / 临时不可用才降级）、空候选 no_compatible_model |
| ③ 重试 | test_retry.py | MockTransport 造超时/429/5xx + fake_sleep 收集延迟：断言每模型重试 1 次（backoff 0.1s）、封顶与 jitter 区间用公式用例单独验证、不可重试零等待；墙钟 ≈ 0 |
| ④ Streaming | test_streaming.py | delta 顺序、usage 事件、终态唯一、错误中断（首块前 fallback / 首块后 failed 无拼接）、facade chunk 序列 + [DONE]、**流式 JSON 状态机**（未闭合不误判、闭合后畸形早失败、流末终校验） |
| ⑤ Structured Output | test_structured_output.py | 围栏剥离/平衡对象提取、**修复次数 ≤ max_strategies**（打桩计数）、全败显式报错、Pydantic 校验 |
| ⑥ 可观测性 | test_observability.py | CallTrace 全字段（两级关联/分阶段延迟/弹性/分项成本/provider_request_ids）、Metrics 快照累加、cached 成本等式 |

- 生产代码两个可测性接缝：`client_factory`（MockTransport）、`sleep` 注入
- test_api.py 为旧 test_gateway.py 用例按新契约迁移（新 traces 结构、新 Usage 字段、chat-fast 模型名）；旧脚本不再直接运行（见 13.2）
- conftest 默认开启 admin_endpoints 便于端点测试，另备关闭配置用例断言 404

## 13. 迁移映射与破坏性变更声明

### 13.1 gateway.py → 新位置映射

| 现有模块 | 去向 |
|---|---|
| Message/Usage/LLMRequest/LLMResponse/CallTrace/PromptSelection | core/schemas.py（扩展：Usage 分项、LLMRequest 增加 model 可选/trace_id、CallTrace 嵌套化） |
| GatewayError | core/errors.py |
| ModelConfig + MODEL_CONFIGS + PRICE_PER_MILLION | core/config.py + config.yaml（pricing 入模型条目，启动校验完整性） |
| Provider 协议 + OpenAICompatibleProvider | core/providers/（base.py + openai_compatible.py，新增客户端缓存与 close()） |
| PROMPT_TEMPLATES + render_prompt + build_messages | core/prompts.py（PromptRegistry + SafePromptRenderer + PromptEngine） |
| validate_model | core/config.py `AppConfig.resolve_model` + router 显式模型硬校验（第 7 节） |
| calculate_cost | core/metrics.py（分项成本） |
| CALL_TRACES + record_trace | core/audit.py（AuditStore + RoutingDecisionLog） |
| is_retryable + call_with_fallback + stream_with_fallback | service/（retry.py + llm_service.py） |
| encode_sse | api/streaming.py（双格式） |
| app + 3 路由 | app.py + api/routes.py（6 端点，3 个管理面受开关控制） |
| 新增 | capabilities/router/health/limits/validation/metrics recorder/Prompt 资产目录/OpenAI facade |
| 移除 | `general-*` 模型命名、旧环境变量覆盖、`"general-backup"` 硬编码、每请求新建客户端 |

### 13.2 破坏性变更声明（v1 兼容承诺已撤销）

1. `/v1/traces` 响应结构重构：平铺字段 `cost_usd/latency_ms/input_tokens/output_tokens` → 嵌套 `cost.*/latency.*/usage.*`；`request_id` → `call_id`；新增 `trace_id`
2. `Usage` 新增 `cached_input_tokens`、`reasoning_tokens` 字段
3. 平台模型名 `general-primary`/`general-backup` 停用，改为业务语义命名 `chat-fast`/`chat-pro`（无别名层）
4. `PRIMARY_PROVIDER_MODEL` / `PRIMARY_BASE_URL` / `BACKUP_PROVIDER_MODEL` / `BACKUP_BASE_URL` 环境变量不再读取（仅 `api_key_env` 生效）
5. `/v1/traces`、`/v1/routing`、`/v1/metrics` 默认不挂载（`admin_endpoints.enabled: false`）
6. `/v1/llm` 的 `stream=true` 由 400 `use_stream_endpoint` 改为直接返回 SSE 流式响应
7. 上游 401/403 的错误码由 `model_unavailable`(502) 改为 `provider_auth_error`(503)
8. 旧测试脚本 `test_gateway.py`/`test_gateway2.py` 不直接运行，用例按新契约迁移至 `tests/test_api.py`

## 14. YAGNI 清单（本期明确不做）

- `responses` 上游协议（本期仅 `chat_completion`，故无协议判别联合、无多协议降级语义问题）
- `tool_calling` 能力（Capability 枚举不含，请求模型无 tools 字段，不透传）
- Anthropic 适配器（注册表为扩展点，本期只注册 openai_compatible）
- run_id/step_id 两级关联（仅 trace_id + call_id，预留扩展位）
- Prompt few_shots / tool_version / eval_gate / changelog 元数据（PromptPackage 已剔除）
- 网关侧租户鉴权/限流 key（无鉴权现状保持；管理面端点默认关闭以收窄暴露面）
- Eval 自动发布流水线
- Prometheus 导出（JSON 快照，实现可替换）
- 持久化存储（审计/路由决策均为内存实现，接口可替换）
- 语义级 Prompt 注入防御（威胁模型：受信内部调用方）

## 15. 关键行为变更（相对旧实现）

**缺陷修复：**

1. 每次调用新建客户端 → 按 (base_url, api_key) 缓存的客户端单例 + 连接池复用（修复基线缺陷 #4）
2. 5xx 从"不可重试"改为可重试
3. 成本表 KeyError（调用成功后抛 500）→ 配置启动校验兜底
4. `"general-backup"` 硬编码 → 配置驱动的候选列表
5. 流式审计补记 usage（上游流末提供时）与 TTFT；L4 失败与配置错误也写入审计
6. 备份模型密钥环境变量修正（基线误用 `DEEPSEEK_API_KEY`）

**语义变更：**

7. 静态主备 fallback → 能力路由（priority/cost/health 排序）
8. `timeout_seconds` 从"单次上游超时"改为"全链路总预算"（含 insufficient_time_budget 429）
9. 显式指定 model 时：不存在 → unknown_model(400)；能力不满足 → structured_output_unsupported(400)；仅临时不可用（熔断/池满/上游故障）才降级到等价候选
10. 增加排队/容量守卫与截止时间预算检查
11. 管理面端点（traces/routing/metrics）默认不挂载，显式开启
