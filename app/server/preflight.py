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
            accessible_root = remote_root
            entries = None
            last_access_error: BaseException | None = None
            for candidate in dict.fromkeys((remote_root, *discovery_roots)):
                try:
                    candidate_entries = self.server.list(candidate)
                except (OSError, PermissionDenied) as exc:
                    last_access_error = exc
                    log_keypoint(logger, "directory_candidate", "warning", error=exc)
                    continue
                accessible_root, entries = candidate, candidate_entries
                break
            if entries is None:
                if last_access_error:
                    raise last_access_error
                raise FileNotFoundError(remote_root)
            detail = f"Read {len(entries)} entries at {accessible_root}"
            if accessible_root != remote_root:
                detail += f" (configured root {remote_root} was not accessible)"
            checks.append(CheckResult("Directory listing", True, detail))
            log_keypoint(logger, checkpoint, "passed")
            checkpoint = "remote_read_access"
            self.server.stat(accessible_root)
            checks.append(CheckResult("Remote read access", True, f"Root metadata is readable at {accessible_root}"))
            log_keypoint(logger, checkpoint, "passed")
            checkpoint = "website_discovery"
            discoverer = SiteDiscoverer(self.server)
            discovery = discoverer.discover(accessible_root)
            if not discovery.wordpress:
                for candidate in dict.fromkeys((remote_root, *discovery_roots)):
                    if candidate == accessible_root:
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
            if discovery.wordpress:
                discovery_detail = discovery.site_root or "WordPress detected"
            else:
                missing = ", ".join(discovery.missing_markers) or "WordPress markers"
                discovery_detail = (
                    f"WordPress not detected after checking {discovery.directories_checked} directories; "
                    f"closest path: {discovery.closest_path or discovery.search_root}; missing: {missing}"
                )
            checks.append(CheckResult("Website discovery", discovery.wordpress, discovery_detail))
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
