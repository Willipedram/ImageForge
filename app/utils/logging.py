"""Application logging with extensible human and structured formatters."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class StructuredFormatter(logging.Formatter):
    """Available for future machine-readable log sinks."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


SECRET_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|token|auth(?:orization)?|api[_-]?key|private[_-]?key)\b\s*[:=]\s*([^\s,;]+)"
)


def redact(message: str) -> str:
    return SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", message)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def configure_logging(project_data: Path, level: str = "INFO") -> Path:
    log_dir = project_data / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "imageforge.log"
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    file_handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.addFilter(RedactingFilter())
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    console = logging.StreamHandler()
    console.addFilter(RedactingFilter())
    console.setFormatter(formatter)
    root.addHandler(console)
    return log_path
