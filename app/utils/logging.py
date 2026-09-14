"""Application logging with extensible human and structured formatters."""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class LiveLogRecord:
    id: int
    created_at: str
    level: str
    logger: str
    thread: str
    message: str


class LiveLogBuffer:
    """Thread-safe bounded stream shared by workers and the Qt log page."""

    def __init__(self, capacity: int = 5_000) -> None:
        self._records: deque[LiveLogRecord] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._next_id = 1

    def append(self, record: logging.LogRecord) -> None:
        with self._lock:
            self._records.append(LiveLogRecord(
                self._next_id,
                datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
                record.levelname,
                record.name,
                record.threadName,
                redact(record.getMessage()),
            ))
            self._next_id += 1

    def read_after(self, record_id: int, limit: int = 200) -> tuple[LiveLogRecord, ...]:
        with self._lock:
            return tuple(record for record in self._records if record.id > record_id)[:limit]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._next_id = 1


LIVE_LOGS = LiveLogBuffer()


class LiveLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LIVE_LOGS.append(record)
        except Exception:
            self.handleError(record)


def read_live_logs(after_id: int = 0, limit: int = 200) -> tuple[LiveLogRecord, ...]:
    return LIVE_LOGS.read_after(after_id, limit)


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
    LIVE_LOGS.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)s | thread=%(threadName)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.addFilter(RedactingFilter())
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    console = logging.StreamHandler()
    console.addFilter(RedactingFilter())
    console.setFormatter(formatter)
    root.addHandler(console)
    live = LiveLogHandler()
    live.addFilter(RedactingFilter())
    root.addHandler(live)
    return log_path
