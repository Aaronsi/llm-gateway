"""L4 输出校验：SchemaValidator（Pydantic 优先 + jsonschema 兜底）、有界修复、流式 JSON 状态机。"""

import json
from dataclasses import dataclass
from typing import Any, Protocol

import jsonschema
import pydantic
from pydantic import TypeAdapter

from app.core.errors import GatewayError


class SchemaValidationError(GatewayError):
    def __init__(self, message: str = "模型结果不符合 response_schema") -> None:
        super().__init__("schema_validation_failed", message, 502)


class UnsupportedKeywordError(Exception):
    pass


class SchemaValidator(Protocol):
    def validate(self, instance: Any, schema: dict[str, Any]) -> Any: ...


# Pydantic 动态转换支持的 JSON Schema 子集；其余关键字走 jsonschema 兜底。
_SUPPORTED_KEYS = {
    "type",
    "properties",
    "required",
    "items",
    "enum",
    "const",
    "additionalProperties",
    "default",
}
_SCALARS: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def _build_validator(schema: dict[str, Any]) -> Any:
    unknown = set(schema) - _SUPPORTED_KEYS
    if unknown:
        raise UnsupportedKeywordError(sorted(unknown))
    stype = schema.get("type")
    if stype in _SCALARS:
        adapter = TypeAdapter(_SCALARS[stype])
        enum_values = schema.get("enum")
        const_value = schema.get("const")

        def validate_scalar(value: Any) -> Any:
            validated = adapter.validate_python(value)
            if const_value is not None and validated != const_value:
                raise ValueError(f"const 不满足: {validated!r}")
            if enum_values is not None and validated not in enum_values:
                raise ValueError(f"enum 不满足: {validated!r}")
            return validated

        return validate_scalar
    if stype == "array":
        item_validator = _build_validator(schema.get("items") or {})

        def validate_list(value: Any) -> Any:
            if not isinstance(value, list):
                raise ValueError("expected list")
            return [item_validator(item) for item in value]

        return validate_list
    if stype == "object":
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        allow_extra = schema.get("additionalProperties", True) is not False
        field_validators = {key: _build_validator(prop) for key, prop in properties.items()}

        def validate_object(value: Any) -> Any:
            if not isinstance(value, dict):
                raise ValueError("expected object")
            if not allow_extra:
                extra = set(value) - set(properties)
                if extra:
                    raise ValueError(f"extra keys: {sorted(extra)}")
            for key in required:
                if key not in value:
                    raise ValueError(f"missing required: {key}")
            result: dict[str, Any] = {}
            for key, item in value.items():
                result[key] = field_validators[key](item) if key in field_validators else item
            return result

        return validate_object
    raise UnsupportedKeywordError([f"type:{stype}"])


class JsonschemaFallbackValidator:
    def validate(self, instance: Any, schema: dict[str, Any]) -> Any:
        try:
            jsonschema.validate(instance=instance, schema=schema)
        except jsonschema.ValidationError as exc:
            raise SchemaValidationError() from exc
        return instance


class PydanticSchemaValidator:
    def __init__(self, fallback: SchemaValidator | None = None) -> None:
        self._fallback = fallback if fallback is not None else JsonschemaFallbackValidator()

    def validate(self, instance: Any, schema: dict[str, Any]) -> Any:
        try:
            validator = _build_validator(schema)
        except UnsupportedKeywordError:
            return self._fallback.validate(instance, schema)
        try:
            return validator(instance)
        except (pydantic.ValidationError, ValueError) as exc:
            raise SchemaValidationError() from exc


def make_schema_validator() -> SchemaValidator:
    return PydanticSchemaValidator()


@dataclass(frozen=True)
class RepairPolicy:
    max_strategies: int = 3


def direct_parse(content: str) -> Any:
    return json.loads(content)


def strip_markdown_fence(content: str) -> Any:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```")
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return json.loads(stripped.strip())


def extract_first_balanced_object(content: str) -> Any:
    start = content.find("{")
    if start == -1:
        raise json.JSONDecodeError("no object found", content, 0)
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(content)):
        ch = content[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(content[start : i + 1])
    raise json.JSONDecodeError("unbalanced object", content, start)


REPAIR_STRATEGIES = [direct_parse, strip_markdown_fence, extract_first_balanced_object]


def repair_json(
    content: str,
    strategies: list = REPAIR_STRATEGIES,
    max_strategies: int = 3,
) -> Any:
    # 逐策略尝试（仅本地文本后处理，绝不触发上游重试）；有界，全败 → invalid_json。
    last_error: Exception | None = None
    for index, strategy in enumerate(strategies):
        if index >= max_strategies:
            break
        try:
            return strategy(content)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
    raise GatewayError("invalid_json", "模型没有返回合法 JSON", 502) from last_error


def validate_output(
    content: str,
    schema: dict[str, Any],
    validator: SchemaValidator | None = None,
    policy: RepairPolicy | None = None,
) -> Any:
    max_strategies = (policy or RepairPolicy()).max_strategies
    parsed = repair_json(content, max_strategies=max_strategies)
    (validator or make_schema_validator()).validate(parsed, schema)
    return parsed


def repair_instruction(error: GatewayError, schema: dict[str, Any]) -> str:
    # LLM feedback 修复指令：要求只返回修正后的合法 JSON，禁止代码块与解释。
    return (
        "你的上一次回答未通过 JSON Schema 校验，请只返回修正后的合法 JSON，"
        "不要包含 Markdown 代码块或任何解释文字。\n"
        f"校验错误: {error.message}\n"
        f"目标 Schema: {json.dumps(schema, ensure_ascii=False)}"
    )


class IncrementalJsonAccumulator:
    # 流式 JSON 状态机：depth/in_string/escape 三态跟踪。
    # 仅当结构闭合（depth==0）时尝试 raw_decode；闭合仍失败 = 硬性畸形，提前终止。
    # 结构未闭合时不解析——合法流式片段必然处于未闭合态，不误判。

    def __init__(self) -> None:
        self._buffer = ""
        self._depth = 0
        self._in_string = False
        self._escape = False
        self._completed: list[Any] = []
        self.invalid = False

    def feed(self, chunk: str) -> None:
        for ch in chunk:
            self._buffer += ch
            if self._escape:
                self._escape = False
                continue
            if ch == "\\" and self._in_string:
                self._escape = True
                continue
            if ch == '"':
                self._in_string = not self._in_string
                continue
            if self._in_string:
                continue
            if ch in "{[":
                self._depth += 1
            elif ch in "}]":
                self._depth -= 1
            if self._depth == 0 and self._buffer.strip():
                try:
                    obj, _ = json.JSONDecoder().raw_decode(self._buffer)
                except json.JSONDecodeError:
                    self.invalid = True
                    return
                self._completed.append(obj)
                self._buffer = ""

    def final(self) -> Any:
        if self.invalid:
            raise GatewayError("invalid_json", "流式 JSON 结构畸形", 502)
        if self._depth != 0:
            raise GatewayError("invalid_json", "JSON 结构未闭合", 502)
        if not self._completed:
            raise GatewayError("invalid_json", "没有收到 JSON 内容", 502)
        return self._completed[-1] if len(self._completed) == 1 else self._completed
