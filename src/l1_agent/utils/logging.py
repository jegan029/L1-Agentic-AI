"""Structured logging with correlation ID support and sensitive field redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="NONE")

SENSITIVE_PATTERNS = [
    re.compile(r"(password|passwd|pwd|secret|token|api_key|apikey|auth)", re.IGNORECASE),
]

REDACTED = "***REDACTED***"


def set_correlation_id(cid: str) -> None:
    _correlation_id.set(cid)


def get_correlation_id() -> str:
    return _correlation_id.get()


def redact_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Redact sensitive fields from a dictionary."""
    redacted: Dict[str, Any] = {}
    for key, value in data.items():
        if any(p.search(key) for p in SENSITIVE_PATTERNS):
            redacted[key] = REDACTED
        elif isinstance(value, dict):
            redacted[key] = redact_dict(value)
        else:
            redacted[key] = value
    return redacted


class StructuredFormatter(logging.Formatter):
    """JSON-lines structured log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": _correlation_id.get(),
        }
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = str(record.exc_info[1])
        if hasattr(record, "extra_data"):
            log_entry["data"] = redact_dict(record.extra_data)
        return json.dumps(log_entry, default=str)


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure the root logger with structured JSON output."""
    logger = logging.getLogger("l1_agent")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter())
        logger.addHandler(handler)
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"l1_agent.{name}")


def log_with_data(
    logger: logging.Logger,
    level: int,
    message: str,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """Log a message with additional structured data."""
    record = logger.makeRecord(
        logger.name, level, "(unknown)", 0, message, (), None
    )
    if data:
        record.extra_data = redact_dict(data)  # type: ignore[attr-defined]
    logger.handle(record)
