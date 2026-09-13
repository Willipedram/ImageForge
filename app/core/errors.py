"""Central exception routing for logs and recoverable UI errors."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from types import TracebackType
from typing import Any


class ErrorHandler:
    def __init__(self, notify: Callable[[str, str], None] | None = None) -> None:
        self.notify = notify
        self.logger = logging.getLogger("imageforge.errors")

    def handle(self, exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        self.logger.critical("Unexpected application error", exc_info=(exc_type, exc, tb))
        if self.notify:
            self.notify("Unexpected error", "ImageForge encountered an error. Details were written to the log.")

    def install(self) -> None:
        sys.excepthook = self.handle


def log_slot_errors(function: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator for future Qt slots whose exceptions Qt might otherwise hide."""
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except Exception:
            logging.getLogger(function.__module__).exception("Recoverable UI action failed")
            raise
    return wrapped
