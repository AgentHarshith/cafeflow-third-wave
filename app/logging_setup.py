"""Logging: structured JSON on Cloud Run (parsed by Cloud Logging), plain text locally.

Cloud Logging reads `severity` and `message` from JSON lines on stdout and indexes the
remaining keys as jsonPayload, so events like `chat_turn` and `order_created` can be
filtered in Logs Explorer (e.g. jsonPayload.event="chat_turn"). Customer message text
is never logged.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
        }
        entry.update(getattr(record, "json_fields", {}) or {})
        if record.exc_info:
            entry["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging() -> None:
    json_logs = os.getenv("LOG_FORMAT", "").lower() == "json" or os.getenv("K_SERVICE") is not None  # set by Cloud Run
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # ADK and HTTP client internals are chatty at INFO/WARNING; keep their errors only.
    for noisy in ("google_adk", "google.adk", "httpx", "google_genai", "google.genai"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


def event(logger: logging.Logger, name: str, message: str, **fields) -> None:
    """Log a structured event (fields become jsonPayload in Cloud Logging)."""
    logger.info(message, extra={"json_fields": {"event": name, **fields}})
