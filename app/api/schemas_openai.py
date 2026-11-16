"""OpenAI Chat Completions facade 模型：请求/响应形状与边界翻译。

- 扩展字段（temperature/top_p/n/stop/seed/user 等）接受后忽略 + debug 日志，任意 OpenAI SDK 调用不炸；
- response_format → response_schema 只在本边界发生（to_internal）；
- to_openai_response 反向翻译，model 字段 = 平台逻辑模型名（绝不暴露 provider_model）。
"""

import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.schemas import LLMRequest, LLMResponse, Message

logger = logging.getLogger("app.access")


class OpenAIMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class JsonSchemaObject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    # alias 避免字段名 schema 遮蔽 BaseModel 属性（pydantic 警告）
    schema_dict: dict[str, Any] = Field(default_factory=dict, alias="schema")
    strict: bool | None = None


class ResponseFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text", "json_object", "json_schema"] = "text"
    json_schema: JsonSchemaObject | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[OpenAIMessage]
    stream: bool = False
    response_format: ResponseFormat | None = None

    @property
    def ignored_fields(self) -> list[str]:
        # 显式枚举被忽略的扩展字段：调用方可见（响应体/日志），消除"设了没生效"困惑。
        return sorted(self.model_extra or {})

    def to_internal(self) -> LLMRequest:
        if self.ignored_fields:
            logger.warning("ignored openai fields: %s", self.ignored_fields)
        response_schema = None
        if self.response_format is not None:
            if self.response_format.type == "json_object":
                response_schema = {"type": "object"}
            elif (
                self.response_format.type == "json_schema"
                and self.response_format.json_schema is not None
            ):
                response_schema = self.response_format.json_schema.schema_dict
        return LLMRequest(
            model=self.model,
            messages=[Message(role=m.role, content=m.content) for m in self.messages],
            stream=self.stream,
            response_schema=response_schema,
        )


def to_openai_response(
    internal: LLMResponse, ignored_fields: list[str] | None = None
) -> dict[str, Any]:
    payload = {
        "id": internal.call_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": internal.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": internal.content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": internal.usage.input_tokens,
            "completion_tokens": internal.usage.output_tokens,
            "total_tokens": internal.usage.input_tokens + internal.usage.output_tokens,
        },
    }
    if ignored_fields:
        payload["ignored_fields"] = ignored_fields
    return payload
