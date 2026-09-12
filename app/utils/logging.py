"""Application logging with extensible human and structured formatters."""

from __future__ import annotations

import json
import logging
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
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


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
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    return log_path
