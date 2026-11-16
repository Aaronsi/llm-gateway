"""领域模型：统一请求/响应契约、用量、调用审计与流式事件。"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Message(BaseModel):
    # 定义跨模型通用的单条对话消息，隔离供应商消息格式差异。
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)


class PromptSelection(BaseModel):
    # 只允许调用方选择受控模板及变量，不能提交或覆盖模板正文。
    # version 未指定时走注册表激活版本（运行时激活，见 S11）。
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    version: str | None = Field(default=None, min_length=1, max_length=50)
    variables: dict[str, str] = Field(default_factory=dict)


class PromptActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=50)


class Usage(BaseModel):
    # 统一输入与输出 Token 统计口径（含缓存与推理分项），用于成本和用量治理。
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)


class LLMRequest(BaseModel):
    # 网关原生统一请求协议；model 缺省走纯能力路由。
    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    messages: list[Message] = Field(min_length=1, max_length=100)
    stream: bool = False
    response_schema: dict[str, Any] | None = None
    prompt: PromptSelection | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    trace_id: str | None = None

    @model_validator(mode="after")
    def check_supported_combination(self) -> "LLMRequest":
        if self.stream and self.response_schema is not None:
            raise ValueError("stream 与 response_schema 不能同时使用")
        return self


class LatencyBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queue_ms: int = 0
    route_ms: int = 0
    ttft_ms: int | None = None
    generation_ms: int = 0
    total_ms: int = 0


class LLMResponse(BaseModel):
    # 统一模型调用结果，并作为 FastAPI 响应出口的 Pydantic 校验契约。
    model_config = ConfigDict(extra="forbid")

    call_id: str
    model: str
    content: str
    parsed: dict[str, Any] | list[Any] | None = None
    usage: Usage
    latency: LatencyBreakdown
    attempts: int = Field(ge=1)


class ResilienceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempts: int = 0
    retries: int = 0
    fallbacks: int = 0
    timeout_budget_seconds: float | None = None
    timeout_consumed_seconds: float = 0


class CostBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_usd: float = 0
    cached_usd: float = 0
    output_usd: float = 0
    total_usd: float = 0


class CallTrace(BaseModel):
    # 保存单次调用的关联、路由、用量、延迟、成本与状态，默认不记录文本内容。
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    call_id: str
    api_key_hash: str | None = None
    requested_model: str | None = None
    actual_model: str | None = None
    provider: str | None = None
    routing_decision_id: str | None = None
    prompt_name: str | None = None
    prompt_version: str | None = None
    prompt_hash: str | None = None
    usage: Usage = Field(default_factory=Usage)
    latency: LatencyBreakdown = Field(default_factory=LatencyBreakdown)
    resilience: ResilienceInfo = Field(default_factory=ResilienceInfo)
    cost: CostBreakdown = Field(default_factory=CostBreakdown)
    finish_reason: str | None = None
    status: Literal["success", "failed", "cancelled"] = "success"
    validation_result: Literal["passed", "failed", "not_applicable"] = "not_applicable"
    repair_attempts: int = 0
    usage_estimated: bool | None = None
    error_code: str | None = None
    http_status: int | None = None
    provider_request_ids: list[str] = Field(default_factory=list)


class StreamDelta(BaseModel):
    type: Literal["delta"] = "delta"
    text: str


class StreamUsageEvent(BaseModel):
    type: Literal["usage"] = "usage"
    usage: Usage
    estimated: bool = False  # True 表示 chars/token 估算，非上游真实统计


class StreamCompleted(BaseModel):
    type: Literal["completed"] = "completed"
    model: str
    parsed: dict[str, Any] | list[Any] | None = None
    usage: Usage


class StreamFailed(BaseModel):
    type: Literal["failed"] = "failed"
    error_code: str
    message: str = ""


class StreamCancelled(BaseModel):
    type: Literal["cancelled"] = "cancelled"


StreamEvent = Annotated[
    StreamDelta | StreamUsageEvent | StreamCompleted | StreamFailed | StreamCancelled,
    Field(discriminator="type"),
]


class CandidateDecision(BaseModel):
    model: str
    order: int | None = None
    reject_reason: str | None = None
    selected: bool = False
    attempts: int = 0
    final_error_code: str | None = None
    weight: int = 1
    rotation_offset: int | None = None


class RoutingDecision(BaseModel):
    call_id: str
    timestamp: Any = None
    config_version: str
    required_capabilities: list[str] = Field(default_factory=list)
    health_snapshot: dict[str, str] = Field(default_factory=dict)
    candidates: list[CandidateDecision] = Field(default_factory=list)
