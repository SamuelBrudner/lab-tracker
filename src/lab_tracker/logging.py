"""Logging configuration for lab tracker."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from lab_tracker.provider_error_redaction import provider_error_message


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": provider_error_message(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = provider_error_message(
                self.formatException(record.exc_info)
            )
        return json.dumps(payload)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
