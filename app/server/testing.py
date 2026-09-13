"""Explicit, auditable server connection diagnostics."""

from __future__ import annotations

import io
from uuid import uuid4

from app.server.base import RemoteServer
from app.server.paths import safe_join
from app.server.preflight import CheckResult


class ConnectionTester:
    def __init__(self, server: RemoteServer) -> None:
        self.server = server

    def test(self, root: str, *, allow_write_test: bool = False) -> tuple[CheckResult, ...]:
        results: list[CheckResult] = []
        self.server.connect()
        results.append(CheckResult("Connection", True, "Transport connected"))
        results.append(CheckResult("Authentication", True, "Authentication accepted"))
        entries = self.server.list(root)
        results.append(CheckResult("Directory listing", True, f"Read {len(entries)} entries"))
        self.server.stat(root)
        results.append(CheckResult("Read permission", True, "Root metadata is readable"))
        if allow_write_test:
            temporary = safe_join(root, f".imageforge-permission-test-{uuid4().hex}.tmp")
            try:
                self.server.upload(io.BytesIO(b"ImageForge permission test\n"), temporary)
                results.append(CheckResult("Write permission", self.server.exists(temporary), "Temporary test succeeded"))
            finally:
                if self.server.exists(temporary):
                    self.server.delete(temporary)
        else:
            results.append(CheckResult("Write permission", True, "Not tested (non-destructive mode)"))
        return tuple(results)
