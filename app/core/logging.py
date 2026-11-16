"""日志基建：ContextVar 关联 ID、脱敏兜底、JSON 格式化与命名 logger 隔离。

白名单原则：调用点不传敏感值（主防线）→ 存储层 schema 即白名单 → SensitiveDataFilter 掩码兜底。
"""

import json
import logging
import re
from contextvars import ContextVar

from app.core.config import LoggingConfig

call_id_var: ContextVar[str | None] = ContextVar("call_id", default=None)
trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)

_MASKED_KEY_SUBSTRINGS = ("key", "token", "secret", "password", "authorization")
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]+"),
    re.compile(r"Bearer\s+[A-Za-z0-9._+-]+"),
)
_MAX_VALUE_LENGTH = 200


class RequestIdFilter(logging.Filter):
    # 中间件写入的 ContextVar 自动附加到每条日志记录。
    def filter(self, record: logging.LogRecord) -> bool:
        record.call_id = call_id_var.get()  # type: ignore[attr-defined]
        record.trace_id = trace_id_var.get()  # type: ignore[attr-defined]
        return True


class SensitiveDataFilter(logging.Filter):
    # 兜底脱敏：掩码密钥类键与 Bearer/sk- 模式，超长字符串截断 200 字符。
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.mask(record.msg)
        return True

    def mask(self, value):
        if isinstance(value, dict):
            return {
                key: (
                    "***"
                    if any(mark in key.lower() for mark in _MASKED_KEY_SUBSTRINGS)
                    else self.mask(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.mask(item) for item in value]
        if isinstance(value, str):
            for pattern in _SECRET_PATTERNS:
                value = pattern.sub("***", value)
            if len(value) > _MAX_VALUE_LENGTH:
                value = value[:_MAX_VALUE_LENGTH] + "..."
            return value
        return value


class JsonFormatter(logging.Formatter):
    # 单行 JSON：dict 消息原样作为 payload，字符串消息经 getMessage 渲染。
    def format(self, record: logging.LogRecord) -> str:
        message = record.msg if isinstance(record.msg, dict) else record.getMessage()
        data = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        for key in ("call_id", "trace_id"):
            value = getattr(record, key, None)
            if value is not None:
                data[key] = value
        return json.dumps(data, ensure_ascii=False, default=str)


_NAMED_LOGGERS = ("access", "error", "audit")


def setup_logging(config: LoggingConfig) -> None:
    # access/error/audit 三个命名 logger：JsonFormatter + 过滤器，propagate=False 隔离根 logger。
    level = getattr(logging, config.level.upper(), logging.INFO)
    for name in _NAMED_LOGGERS:
        logger = logging.getLogger(f"app.{name}")
        logger.handlers.clear()
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler.addFilter(RequestIdFilter())
        handler.addFilter(SensitiveDataFilter())
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
