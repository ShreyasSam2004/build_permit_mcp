"""JSON-lines logging to stderr.

stdout belongs to the stdio transport, and spec 2026-07-28 deprecates MCP-level logging in
favor of stderr/OpenTelemetry, so all logs go to stderr as one JSON object per line.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD})
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure(level: str = "INFO") -> None:
    """Install the JSON handler on the root logger. Call before building the MCPServer, whose
    constructor runs ``logging.basicConfig`` (a no-op once a handler exists)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request is noise
