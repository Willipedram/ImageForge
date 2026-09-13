"""Non-destructive connection and job preflight checks."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.server.base import RemoteServer
from app.server.discovery import SiteDiscoverer, SiteDiscovery
from app.server.errors import PermissionDenied
from app.utils.checkpoints import log_keypoint

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class PreflightReport:
    checks: tuple[CheckResult, ...]
    discovery: SiteDiscovery | None

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks if check.name != "Database access")


class PreflightService:
    def __init__(self, server: RemoteServer, project_data: Path, minimum_free_bytes: int = 256 * 1024 * 1024) -> None:
        self.server = server
        self.project_data = project_data
        self.minimum_free_bytes = minimum_free_bytes

    def run(self, remote_root: str, discovery_roots: tuple[str, ...] = ()) -> PreflightReport:
        checks: list[CheckResult] = []
        discovery = None
        checkpoint = "connection"
        try:
            if not self.server.connected:
                self.server.connect()
            checks.append(CheckResult("Connection and authentication", True, "Connected"))
            log_keypoint(logger, checkpoint, "passed")
            checkpoint = "directory_listing"
            entries = self.server.list(remote_root)
            checks.append(CheckResult("Directory listing", True, f"Read {len(entries)} entries"))
            log_keypoint(logger, checkpoint, "passed")
            checkpoint = "remote_read_access"
            self.server.stat(remote_root)
            checks.append(CheckResult("Remote read access", True, "Root metadata is readable"))
            log_keypoint(logger, checkpoint, "passed")
            checkpoint = "website_discovery"
            discoverer = SiteDiscoverer(self.server)
            discovery = discoverer.discover(remote_root)
            if not discovery.wordpress:
                for candidate in discovery_roots:
                    if candidate == remote_root:
                        continue
                    try:
                        self.server.stat(candidate)
                        alternative = discoverer.discover(candidate)
                    # Hosting accounts often expose only their assigned root
                    # and reject probes of unrelated conventional paths.
                    # A denied fallback is not a failure of the valid login.
                    except (OSError, PermissionDenied):
                        continue
                    if alternative.wordpress:
                        discovery = alternative
                        break
            checks.append(CheckResult("Website discovery", discovery.wordpress, discovery.site_root or "WordPress not detected"))
            log_keypoint(logger, checkpoint, "passed" if discovery.wordpress else "stopped")
        except Exception as exc:
            log_keypoint(logger, checkpoint, "stopped", error=exc)
            checks.append(CheckResult("Remote access", False, f"Stopped at {checkpoint}: {exc}"))
        self.project_data.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.project_data).free
        checks.append(CheckResult("Local disk space", free >= self.minimum_free_bytes, f"{free} bytes available"))
        checks.append(CheckResult("ProjectData", self.project_data.is_dir(), str(self.project_data)))
        checks.append(CheckResult("Database access", True, "Deferred to a future phase"))
        return PreflightReport(tuple(checks), discovery)
