"""Bounded reconnect behavior shared across protocols."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from app.core.retry import RetryPolicy
from app.server.base import RemoteServer
from app.server.errors import AuthenticationError, PermissionDenied

T = TypeVar("T")


class ResilientServer:
    """Retries one operation over one connection; never creates connection pools."""

    def __init__(self, server: RemoteServer, policy: RetryPolicy | None = None,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        self.server = server
        self.policy = policy or RetryPolicy()
        self.sleeper = sleeper

    def call(self, operation: Callable[[], T]) -> T:
        last_error: Exception | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                if not self.server.connected:
                    self.server.connect()
                return operation()
            except (AuthenticationError, PermissionDenied):
                raise
            except Exception as exc:
                last_error = exc
                self.server.disconnect()
                if attempt < self.policy.max_attempts:
                    self.sleeper(self.policy.delay_for(attempt))
        assert last_error is not None
        raise last_error

    def disconnect(self) -> None:
        self.server.disconnect()
