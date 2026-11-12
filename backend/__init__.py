"""Amazon PL Hunter v2 — backend package.

Structured logging helper lives here so every module can share one
JSON-ish format without a circular import (db.py imports nothing else
from this package).
"""
from __future__ import annotations

import json
import logging
import sys


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "module": record.name,
            "run_id": getattr(record, "run_id", None),
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO", stream=None) -> None:
    # Logs go to stderr by default so stdout stays clean for machine-readable
    # output (e.g. scripts/run_hunt.py prints the run summary JSON on stdout).
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_logger(name: str, run_id=None) -> logging.LoggerAdapter:
    logger = logging.getLogger(name)
    return logging.LoggerAdapter(logger, {"run_id": run_id})


__all__ = ["setup_logging", "get_logger"]
__version__ = "2.0.0"
