"""Structured JSON logging with secret redaction (Phase 11: "Redact secrets
and personal credentials from logs"). Redaction is applied to every field
value, not just known keys, since a secret can end up embedded in an error
message or a raw broker/OpenAI response string rather than in a field
that's obviously named "token" or "key".
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Patterns matched against any string value before it's written to a log
# record. Deliberately broad (bearer tokens, OANDA practice tokens which are
# long hex strings, OpenAI keys which start with sk-) rather than an
# exhaustive enumeration of every secret shape.
_REDACTION_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{10,}", re.IGNORECASE),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),  # long hex tokens (OANDA personal access tokens, etc.)
]


def redact(value: Any) -> Any:
    if isinstance(value, str):
        redacted = value
        for pattern in _REDACTION_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class _RedactingJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(redact(extra))
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class ContextLogger(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        extra_fields = {k: v for k, v in kwargs.items() if k not in ("exc_info", "stack_info")}
        for key in list(kwargs):
            if key not in ("exc_info", "stack_info"):
                kwargs.pop(key)
        if extra_fields:
            kwargs["extra"] = {"extra_fields": extra_fields}
        return msg, kwargs


_CONFIGURED = False


def configure_logging(level: str = "INFO", directory: str | None = "logs") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger("x_copy_trade_tool")
    root.setLevel(level)
    root.propagate = False

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(_RedactingJsonFormatter())
    root.addHandler(stream_handler)

    if directory:
        log_dir = Path(directory)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "app.log", encoding="utf-8")
        file_handler.setFormatter(_RedactingJsonFormatter())
        root.addHandler(file_handler)

    _CONFIGURED = True


def get_logger(name: str) -> ContextLogger:
    if not _CONFIGURED:
        configure_logging()
    return ContextLogger(logging.getLogger(f"x_copy_trade_tool.{name}"), {})
