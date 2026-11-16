"""⑤ Structured Output：修复策略（有界）、校验两层、不可重试边界。"""

import json

import pytest

from app.core.errors import GatewayError
from app.core.validation import (
    REPAIR_STRATEGIES,
    make_schema_validator,
    repair_json,
    validate_output,
)

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def test_direct_parse_succeeds() -> None:
    assert repair_json('{"answer": "ok"}') == {"answer": "ok"}


def test_markdown_fence_is_stripped() -> None:
    content = '```json\n{"answer": "ok"}\n```'
    assert repair_json(content) == {"answer": "ok"}


def test_extract_first_balanced_object_from_prose() -> None:
    content = '好的，结果如下：{"answer": "ok"} 希望有帮助。'
    assert repair_json(content) == {"answer": "ok"}


def test_repair_strategies_are_tried_in_order_and_bounded() -> None:
    attempts: list[str] = []

    def spy(name: str):
        def strategy(text: str):
            attempts.append(name)
            raise json.JSONDecodeError("nope", text, 0)

        return strategy

    with pytest.raises(GatewayError) as exc_info:
        repair_json(
            "not json at all",
            strategies=[spy("a"), spy("b"), spy("c")],
            max_strategies=3,
        )
    assert exc_info.value.code == "invalid_json"
    assert exc_info.value.status_code == 502
    assert attempts == ["a", "b", "c"]


def test_repair_stops_at_max_strategies() -> None:
    attempts = []

    def spy(name: str):
        def strategy(text: str):
            attempts.append(name)
            raise json.JSONDecodeError("nope", text, 0)

        return strategy

    with pytest.raises(GatewayError):
        repair_json(
            "bad",
            strategies=[spy("a"), spy("b"), spy("c"), spy("d")],
            max_strategies=2,
        )
    assert attempts == ["a", "b"]


def test_default_strategies_cover_fence_and_prose() -> None:
    assert len(REPAIR_STRATEGIES) == 3
    assert repair_json('```json\n{"answer": "x"}\n```') == {"answer": "x"}


def test_validate_output_passes_valid_instance() -> None:
    parsed = validate_output('{"answer": "ok"}', SCHEMA)
    assert parsed == {"answer": "ok"}


def test_schema_validation_failure_raises_502() -> None:
    with pytest.raises(GatewayError) as exc_info:
        validate_output('{"answer": 1}', SCHEMA)
    assert exc_info.value.code == "schema_validation_failed"
    assert exc_info.value.status_code == 502


def test_invalid_json_raises_before_schema_validation() -> None:
    with pytest.raises(GatewayError) as exc_info:
        validate_output("not json", SCHEMA)
    assert exc_info.value.code == "invalid_json"


def test_pydantic_unsupported_keyword_falls_back_to_jsonschema() -> None:
    validator = make_schema_validator()
    pattern_schema = {
        "type": "object",
        "properties": {"code": {"type": "string", "pattern": "^[A-Z]{3}$"}},
        "required": ["code"],
    }
    assert validator.validate({"code": "ABC"}, pattern_schema) == {"code": "ABC"}
    with pytest.raises(GatewayError) as exc_info:
        validator.validate({"code": "abc"}, pattern_schema)
    assert exc_info.value.code == "schema_validation_failed"


def test_validator_returns_normalized_instance() -> None:
    validator = make_schema_validator()
    result = validator.validate({"answer": "ok"}, SCHEMA)
    assert result == {"answer": "ok"}
