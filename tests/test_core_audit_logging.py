"""日志基建：脱敏、JSON 格式化、ContextVar 关联 ID、命名 logger 隔离。"""

import json
import logging

from app.core.config import LoggingConfig
from app.core.logging import (
    JsonFormatter,
    RequestIdFilter,
    SensitiveDataFilter,
    call_id_var,
    setup_logging,
    trace_id_var,
)


def _record(message, call_id=None, trace_id=None) -> logging.LogRecord:
    record = logging.LogRecord("app.access", logging.INFO, __file__, 1, message, (), None)
    record.call_id = call_id
    record.trace_id = trace_id
    return record


class TestSensitiveDataFilter:
    def test_masks_secret_keys(self) -> None:
        mask = SensitiveDataFilter()
        masked = mask.mask({"api_key": "sk-abc123", "authorization": "Bearer tok", "ok": "fine"})
        assert masked["api_key"] == "***"
        assert masked["authorization"] == "***"
        assert masked["ok"] == "fine"

    def test_masks_secrets_embedded_in_strings(self) -> None:
        mask = SensitiveDataFilter()
        masked = mask.mask("curl -H 'Authorization: Bearer xyz'")
        assert masked == "curl -H 'Authorization: ***'"

    def test_truncates_long_values(self) -> None:
        mask = SensitiveDataFilter()
        assert mask.mask({"content": "x" * 300})["content"] == "x" * 200 + "..."

    def test_filter_rewrites_dict_messages_without_mutating_source(self) -> None:
        mask = SensitiveDataFilter()
        original = {"api_key": "sk-abc123"}
        record = _record(original)
        assert mask.filter(record) is True
        assert record.msg["api_key"] == "***"
        assert original["api_key"] == "sk-abc123"


class TestRequestIdFilter:
    def test_attaches_contextvars_to_record(self) -> None:
        token1 = call_id_var.set("call-1")
        token2 = trace_id_var.set("trace-1")
        try:
            record = _record("hello")
            assert RequestIdFilter().filter(record) is True
            assert record.call_id == "call-1"
            assert record.trace_id == "trace-1"
        finally:
            call_id_var.reset(token1)
            trace_id_var.reset(token2)

    def test_no_context_leaves_record_untouched(self) -> None:
        record = _record("hello")
        assert RequestIdFilter().filter(record) is True
        assert record.call_id is None
        assert record.trace_id is None


class TestJsonFormatter:
    def test_formats_record_as_json_with_ids(self) -> None:
        formatter = JsonFormatter()
        data = json.loads(formatter.format(_record("hello", call_id="c1", trace_id="t1")))
        assert data["message"] == "hello"
        assert data["level"] == "INFO"
        assert data["logger"] == "app.access"
        assert data["call_id"] == "c1"
        assert data["trace_id"] == "t1"
        assert "timestamp" in data

    def test_formats_dict_message_as_json_payload(self) -> None:
        formatter = JsonFormatter()
        data = json.loads(formatter.format(_record({"event": "done", "count": 3})))
        assert data["message"] == {"event": "done", "count": 3}


class TestSetupLogging:
    def test_isolates_named_loggers(self) -> None:
        setup_logging(LoggingConfig())
        for name in ("access", "error", "audit"):
            logger = logging.getLogger(f"app.{name}")
            assert logger.propagate is False
            assert logger.handlers, f"{name} 应有 handler"
            assert isinstance(logger.handlers[0].formatter, JsonFormatter)
