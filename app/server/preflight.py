"""Non-destructive connection and job preflight checks."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.server.base import RemoteServer
from app.server.discovery import DiscoveryTrace, SiteDiscoverer, SiteDiscovery
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
        run_id = uuid4().hex[:12]
        try:
            log_keypoint(logger, checkpoint, "started", context={"run": run_id, "root": remote_root})
            if not self.server.connected:
                self.server.connect()
            checks.append(CheckResult("Connection and authentication", True, "Connected"))
            log_keypoint(logger, checkpoint, "passed", context={"run": run_id})
            checkpoint = "directory_listing"
            accessible_root = remote_root
            entries = None
            last_access_error: BaseException | None = None
            for candidate in dict.fromkeys((remote_root, *discovery_roots)):
                try:
                    log_keypoint(logger, "directory_candidate", "started",
                                 context={"run": run_id, "path": candidate})
                    candidate_entries = self.server.list(candidate)
                except (OSError, PermissionDenied) as exc:
                    last_access_error = exc
                    log_keypoint(logger, "directory_candidate", "warning", error=exc,
                                 context={"run": run_id, "path": candidate, "next": "trying next root"})
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
            log_keypoint(logger, checkpoint, "passed", context={
                "run": run_id, "path": accessible_root, "entries": len(entries),
                "fallback_used": accessible_root != remote_root,
            })
            checkpoint = "remote_read_access"
            self.server.stat(accessible_root)
            checks.append(CheckResult("Remote read access", True, f"Root metadata is readable at {accessible_root}"))
            log_keypoint(logger, checkpoint, "passed", context={"run": run_id, "path": accessible_root})
            checkpoint = "website_discovery"
            log_keypoint(logger, checkpoint, "started", context={"run": run_id, "path": accessible_root})
            discovery_traces: list[DiscoveryTrace] = []
            def trace_directory(trace: DiscoveryTrace) -> None:
                discovery_traces.append(trace)
                children = trace.child_directories[:30]
                trace_status = (
                    "warning" if trace.status == "denied"
                    else "skipped" if trace.status.startswith("skipped_")
                    else "inspected"
                )
                log_keypoint(
                    logger, "website_discovery_directory",
                    trace_status,
                    context={
                        "run": run_id, "path": trace.path, "depth": trace.depth,
                        "result": trace.status, "entries": trace.entry_count,
                        "child_directories": ", ".join(children),
                        "children_truncated": len(trace.child_directories) > len(children),
                        "found_markers": ", ".join(trace.found_markers),
                        "missing_markers": ", ".join(trace.missing_markers),
                        "error_type": trace.error_type,
                    },
                )

            discoverer = SiteDiscoverer(self.server, trace=trace_directory)
            discovery = discoverer.discover(accessible_root)
            if not discovery.wordpress:
                for candidate in dict.fromkeys((remote_root, *discovery_roots)):
                    if candidate == accessible_root or any(trace.path == candidate for trace in discovery_traces):
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
            inspected_paths = list(dict.fromkeys(
                trace.path for trace in discovery_traces
                if trace.status in {"inspected", "wordpress_found"}
            ))
            skipped_paths = list(dict.fromkeys(
                f"{trace.path} ({trace.status.removeprefix('skipped_')})"
                for trace in discovery_traces if trace.status.startswith("skipped_")
            ))
            denied_paths = list(dict.fromkeys(
                trace.path for trace in discovery_traces if trace.status == "denied"
            ))
            all_empty_paths = list(dict.fromkeys(
                trace.path for trace in discovery_traces
                if trace.status == "inspected" and trace.entry_count == 0
            ))
            if discovery.wordpress:
                discovery_detail = discovery.site_root or "WordPress detected"
            else:
                missing = ", ".join(discovery.missing_markers) or "WordPress markers"
                discovery_detail = (
                    f"WordPress not detected after checking {discovery.directories_checked} directories; "
                    f"closest path: {discovery.closest_path or discovery.search_root}; missing: {missing}; "
                    f"empty directories: {', '.join(discovery.empty_directories) or 'none'}"
                )
            checks.append(CheckResult("Website discovery", discovery.wordpress, discovery_detail))
            conclusion = (
                "WordPress root found"
                if discovery.wordpress
                else "FTP-visible public_html is empty; site files are outside this account root"
                if "/public_html" in all_empty_paths
                else "No inspected directory contains all required WordPress markers"
            )
            log_keypoint(logger, "website_discovery_summary",
                         "passed" if discovery.wordpress else "stopped", context={
                "run": run_id,
                "inspected_paths": " -> ".join(inspected_paths) or "none",
                "skipped_paths": ", ".join(skipped_paths) or "none",
                "denied_paths": ", ".join(denied_paths) or "none",
                "empty_paths": ", ".join(all_empty_paths) or "none",
                "conclusion": conclusion,
                "recommended_action": "locate wp-config.php in DirectAdmin File Manager and update FTP root"
                    if not discovery.wordpress else "continue",
            })
            log_keypoint(logger, checkpoint, "passed" if discovery.wordpress else "stopped", context={
                "run": run_id,
                "search_root": discovery.search_root,
                "site_root": discovery.site_root,
                "closest_path": discovery.closest_path,
                "missing_markers": ", ".join(discovery.missing_markers),
                "directories_checked": discovery.directories_checked,
                "empty_directories": ", ".join(discovery.empty_directories),
                "next": "check DirectAdmin FTP account root" if not discovery.wordpress else None,
            })
        except Exception as exc:
            log_keypoint(logger, checkpoint, "stopped", error=exc,
                         context={"run": run_id, "root": remote_root})
            checks.append(CheckResult("Remote access", False, f"Stopped at {checkpoint}: {exc}"))
        self.project_data.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.project_data).free
        checks.append(CheckResult("Local disk space", free >= self.minimum_free_bytes, f"{free} bytes available"))
        checks.append(CheckResult("ProjectData", self.project_data.is_dir(), str(self.project_data)))
        checks.append(CheckResult("Database access", True, "Deferred to a future phase"))
        return PreflightReport(tuple(checks), discovery)
