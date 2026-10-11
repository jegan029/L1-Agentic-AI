"""Structured logging with correlation ID support and sensitive data redaction.

Redaction covers structured data by key name and free text by pattern, so a
secret that ends up in a log message or an exception string is masked too.
"""

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

# Free-text patterns. Each replacement keeps enough context to stay readable.
_TEXT_PATTERNS = [
    # Authorization schemes: "Bearer abc...", "Basic abc..."
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"), r"\1 " + REDACTED),
    # key=value / key: value / "key": "value" for sensitive key names
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|client_secret|token|access_token|api[_-]?key"
            r"|apikey|authorization)\b(\"?\s*[=:]\s*\"?)(?!\*\*\*)([^\s\"',;&]+)"
        ),
        r"\1\2" + REDACTED,
    ),
    # Credentials embedded in a URL
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1" + REDACTED + "@"),
    # Well-known token shapes
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), REDACTED),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        REDACTED,
    ),
]
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")


def set_correlation_id(cid: str) -> None:
    _correlation_id.set(cid)


def get_correlation_id() -> str:
    return _correlation_id.get()


def redact_text(text: str, *, personal_data: bool = False) -> str:
    """Mask secrets in free text. With ``personal_data`` also mask email addresses.

    Pattern based, so it reduces exposure and does not guarantee it.
    """
    if not isinstance(text, str) or not text:
        return text
    for pattern, replacement in _TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    if personal_data:
        text = _EMAIL.sub("***EMAIL***", text)
    return text


def redact_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Redact sensitive fields from a dictionary, by key name and by value pattern."""
    redacted: Dict[str, Any] = {}
    for key, value in data.items():
        if any(p.search(str(key)) for p in SENSITIVE_PATTERNS):
            redacted[key] = REDACTED
        elif isinstance(value, dict):
            redacted[key] = redact_dict(value)
        elif isinstance(value, str):
            redacted[key] = redact_text(value)
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
            "message": redact_text(record.getMessage()),
            "correlation_id": _correlation_id.get(),
        }
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = redact_text(str(record.exc_info[1]))
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
    record = logger.makeRecord(logger.name, level, "(unknown)", 0, message, (), None)
    if data:
        record.extra_data = redact_dict(data)  # type: ignore[attr-defined]
    logger.handle(record)
