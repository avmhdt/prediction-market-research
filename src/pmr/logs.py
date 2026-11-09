"""structlog configuration: JSON lines with secrets redacted (NFR-07, NFR-09)."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_SECRET_KEYS = ("token", "authorization", "api_key", "apikey", "signature", "private_key")


def _redact(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event):
        if any(s in key.lower() for s in _SECRET_KEYS):
            event[key] = "***"
    return event


def configure(level: str = "INFO", *, json: bool = True) -> None:
    """Configure structlog to write one JSON object per line to stderr."""
    lvl = getattr(logging, level.upper(), logging.INFO)
    renderer: Any = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
