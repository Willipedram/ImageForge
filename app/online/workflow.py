"""Non-destructive, checkpointed production website optimization pipeline."""

from __future__ import annotations

import hashlib
import os
import posixpath
import shutil
import time
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from uuid import NAMESPACE_URL, uuid5

from app.core.engine import JobEngine
from app.core.jobs import JobStatus
from app.core.resources import ResourceManager
from app.database.online import OnlineRepository
from app.image.intelligence import detect_format
from app.image.optimization_models import OptimizationDecision
from app.image.optimizer import OfflineOptimizer
from app.online.models import OnlineItem, OnlineItemStatus, OnlineReport, ReferenceChange, ReferenceUpdater
from app.server.base import RemoteServer
from app.server.discovery import RemoteImageScanner, SiteDiscoverer
from app.server.paths import normalize_remote_path, safe_join
from app.server.preflight import PreflightService


class _RetryingServer(RemoteServer):
    """RemoteServer view that adds workflow reconnect semantics to scanners."""

    def __init__(self, workflow: "OnlineWorkflow") -> None: self.workflow = workflow
    @property
    def connected(self): return self.workflow.server.connected
    def connect(self): return self.workflow._remote(None, self.workflow.server.connect)
    def disconnect(self): return self.workflow.server.disconnect()
    def list(self, path): return self.workflow._remote(None, lambda: self.workflow.server.list(path))
    def stat(self, path): return self.workflow._remote(None, lambda: self.workflow.server.stat(path))
    def download(self, remote_path, destination): return self.workflow._remote(None, lambda: self.workflow.server.download(remote_path, destination))
    def upload(self, source, remote_path): return self.workflow._remote(None, lambda: self.workflow.server.upload(source, remote_path))
    def delete(self, path): return self.workflow._remote(None, lambda: self.workflow.server.delete(path))
    def rename(self, source, destination): return self.workflow._remote(None, lambda: self.workflow.server.rename(source, destination))
    def exists(self, path): return self.workflow._remote(None, lambda: self.workflow.server.exists(path))
    def mkdir(self, path): return self.workflow._remote(None, lambda: self.workflow.server.mkdir(path))
    def checksum(self, path, algorithm="sha256"): return self.workflow._remote(None, lambda: self.workflow.server.checksum(path, algorithm))
    def read_prefix(self, path, maximum_bytes): return self.workflow._remote(None, lambda: self.workflow.server.read_prefix(path, maximum_bytes))


class OnlineWorkflow:
    """Coordinates remote I/O in small durable units; originals are never deleted."""

    def __init__(self, project_data: Path, engine: JobEngine, server: RemoteServer,
                 optimizer: OfflineOptimizer, updater: ReferenceUpdater,
                 repository: OnlineRepository | None = None,
                 sleeper: Callable[[float], None] = time.sleep, finalizer=None,
                 resources: ResourceManager | None = None) -> None:
        self.project_data = project_data.resolve()
        self.engine, self.server, self.optimizer, self.updater = engine, server, optimizer, updater
        self.repository = repository or OnlineRepository(engine.repository)
        self.sleeper = sleeper
        self.finalizer = finalizer
        self.resources = resources

    def create(self, target: str, remote_root: str = "/") -> str:
        root = normalize_remote_path(remote_root)
        job = self.engine.create_job(f"remote:{target.strip().casefold()}:{root}")
        staging = safe_join(root, "optimization-temp", job.id)
        self.repository.create_run(job.id, root, staging)
        self.engine.transition(job.id, JobStatus.PRECHECK)
        return job.id

    def run(self, job_id: str, progress: Callable[[str, int, int, str], None] | None = None) -> OnlineReport:
        """Run or resume the full pipeline from persisted item state."""
        try:
            job = self.engine.repository.get(job_id)
            if job.status in {JobStatus.PAUSED, JobStatus.RECOVERABLE}:
                self.reconcile(job_id)
                self.engine.resume(job_id)
            self._precheck_discover_scan(job_id, progress)
            self._download(job_id, progress)
            self._optimize(job_id, progress)
            self._backup(job_id, progress)
            self._upload_verify_promote(job_id, progress)
            self._database_update(job_id, progress)
            self._final_verify(job_id, progress)
            self._cleanup(job_id)
            return self.repository.report(job_id)
        finally:
            self.server.disconnect()

    def pause(self, job_id: str) -> None:
        self.engine.pause(job_id)

    def cancel(self, job_id: str) -> None:
        # Uploaded sidecars and local backups are retained for audit/recovery.
        self.engine.cancel(job_id)

    def reconcile(self, job_id: str) -> None:
        """Inspect external state instead of assuming an interrupted operation failed."""
        active = {OnlineItemStatus.DOWNLOADING, OnlineItemStatus.UPLOADING,
                  OnlineItemStatus.STAGED, OnlineItemStatus.REMOTE_VERIFIED,
                  OnlineItemStatus.PROMOTED, OnlineItemStatus.DB_PENDING,
                  OnlineItemStatus.DB_UPDATED}
        for item in self.repository.iter_items(job_id, active):
            if item.status is OnlineItemStatus.DOWNLOADING:
                local = Path(item.local_path or "")
                item.status = (OnlineItemStatus.DOWNLOADED if local.is_file() and
                               self._checksum(local) == item.local_checksum else OnlineItemStatus.DISCOVERED)
            elif item.production_path and self._remote_matches(item, item.production_path):
                item.status = (OnlineItemStatus.DB_UPDATED if item.database_status == "UPDATED"
                               else OnlineItemStatus.PROMOTED)
                item.upload_status, item.verification_status = "UPLOADED", "VERIFIED"
            elif item.staging_path and self._remote_matches(item, item.staging_path):
                item.status = OnlineItemStatus.STAGED
                item.upload_status = "UPLOADED"
            elif item.status in {OnlineItemStatus.UPLOADING, OnlineItemStatus.STAGED,
                                OnlineItemStatus.REMOTE_VERIFIED}:
                item.status, item.upload_status = OnlineItemStatus.READY, "NOT_STARTED"
            self.repository.save_item(item)
            self._checkpoint(job_id, "online_reconcile", item, safe=True)

    def manifest(self, job_id: str) -> Iterator[OnlineItem]:
        return self.repository.iter_items(job_id)

    def _precheck_discover_scan(self, job_id: str, progress) -> None:
        job, run = self.engine.repository.get(job_id), self._run(job_id)
        if job.status is not JobStatus.PRECHECK:
            return
        report = self._remote(None, lambda: PreflightService(self.server, self.project_data).run(run["remote_root"]))
        if not report.passed:
            raise RuntimeError("Online preflight failed: " + "; ".join(c.detail for c in report.checks if not c.passed))
        discovery = report.discovery or self._remote(None, lambda: SiteDiscoverer(self.server).discover(run["remote_root"]))
        if not discovery.wordpress or not discovery.site_root or not discovery.uploads:
            raise RuntimeError("A WordPress uploads directory could not be discovered safely.")
        self.repository.set_discovery(job_id, discovery.site_root, discovery.uploads)
        self._checkpoint(job_id, "online_precheck_complete", payload={"site_root": discovery.site_root})
        self.engine.transition(job_id, JobStatus.SCANNING)
        batch: list[OnlineItem] = []
        scanner = RemoteImageScanner(_RetryingServer(self))
        # Scanner is lazy and stores batches rather than retaining the site inventory.
        for remote in scanner.scan(discovery.uploads):
            if not self._may_continue(job_id): break
            identifier = str(uuid5(NAMESPACE_URL, f"{job_id}:{remote.path}"))
            batch.append(OnlineItem(identifier, job_id, remote.path, remote.size or 0))
            if len(batch) >= 200:
                self.repository.save_items(batch); batch.clear()
        self.repository.save_items(batch)
        total = self.repository.count(job_id)
        originals = sum(i.original_bytes for i in self.repository.iter_items(job_id))
        self.engine.update_statistics(job_id, files_total=total, files_completed=0,
                                      original_bytes=originals, optimized_bytes=0, progress=0)
        self._checkpoint(job_id, "online_scan_complete", payload={"total": total})
        self.engine.transition(job_id, JobStatus.DOWNLOADING)

    def _download(self, job_id: str, progress) -> None:
        if self.engine.repository.get(job_id).status is not JobStatus.DOWNLOADING: return
        items = {OnlineItemStatus.DISCOVERED, OnlineItemStatus.DOWNLOADING}
        total = self.repository.count(job_id); done = total - sum(1 for _ in self.repository.iter_items(job_id, items))
        root = self.project_data / "jobs" / job_id / "downloaded"
        for item in self.repository.iter_items(job_id, items):
            if not self._may_continue(job_id): return
            local = root / self._relative(item.remote_path, self._run(job_id)["uploads_root"])
            local.parent.mkdir(parents=True, exist_ok=True)
            part = local.with_name(local.name + ".part")
            item.status, item.local_path = OnlineItemStatus.DOWNLOADING, str(local)
            self.repository.save_item(item)
            if self.resources:
                with self.resources.reserve_pool("download"):
                    self._remote(item, lambda: self.server.download(item.remote_path, part))
            else:
                self._remote(item, lambda: self.server.download(item.remote_path, part))
            local_hash = self._checksum(part)
            remote_hash = self._remote(item, lambda: self.server.checksum(item.remote_path))
            if remote_hash and remote_hash.casefold() != local_hash:
                part.unlink(missing_ok=True); raise IOError("Downloaded checksum does not match the remote original.")
            os.replace(part, local)
            item.original_checksum = remote_hash or local_hash
            item.local_checksum, item.original_bytes = local_hash, local.stat().st_size
            item.status = OnlineItemStatus.DOWNLOADED
            self.repository.save_item(item); self._checkpoint(job_id, "download_verified", item)
            done += 1; self._progress(progress, "Downloading", done, total, item.remote_path)
        self.engine.transition(job_id, JobStatus.OPTIMIZING)

    def _optimize(self, job_id: str, progress) -> None:
        if self.engine.repository.get(job_id).status is not JobStatus.OPTIMIZING: return
        pending = {OnlineItemStatus.DOWNLOADED, OnlineItemStatus.OPTIMIZING}
        total = self.repository.count(job_id); done = total - sum(1 for _ in self.repository.iter_items(job_id, pending))
        for item in self.repository.iter_items(job_id, pending):
            if not self._may_continue(job_id): return
            item.status = OnlineItemStatus.OPTIMIZING; self.repository.save_item(item)
            result = self.optimizer.optimize(job_id, Path(item.local_path), item.remote_path)
            item.decision, item.decision_reason = result.decision.value, result.decision_reason
            if result.decision is OptimizationDecision.SELECTED and result.candidate_path:
                item.status, item.candidate_path = OnlineItemStatus.READY, result.candidate_path
                item.candidate_format, item.candidate_bytes = result.candidate_format, result.candidate_bytes
                item.candidate_checksum = result.checksum
            else:
                item.status = OnlineItemStatus.SKIPPED
            self.repository.save_item(item); self._checkpoint(job_id, "quality_decision_persisted", item)
            done += 1; self._progress(progress, "Optimizing", done, total, item.remote_path)
        self.engine.transition(job_id, JobStatus.VALIDATING)

    def _backup(self, job_id: str, progress) -> None:
        if self.engine.repository.get(job_id).status is not JobStatus.VALIDATING: return
        states = {OnlineItemStatus.READY, OnlineItemStatus.BACKED_UP}
        total = self.repository.count_statuses(job_id, states)
        for number, item in enumerate(self.repository.iter_items(job_id, states), 1):
            if not self._may_continue(job_id): return
            source = Path(item.local_path)
            if self._checksum(source) != item.local_checksum: raise IOError("Local original changed before backup.")
            backup = self.project_data / "backups" / "online" / job_id / self._relative(item.remote_path, self._run(job_id)["uploads_root"])
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists(): shutil.copy2(source, backup)
            if self._checksum(backup) != item.local_checksum: raise IOError("Online backup verification failed.")
            item.status = OnlineItemStatus.BACKED_UP
            self.repository.save_item(item); self._checkpoint(job_id, "online_backup_verified", item)
            self._progress(progress, "Backup", number, total, item.remote_path)
        self.engine.transition(job_id, JobStatus.UPLOADING)

    def _upload_verify_promote(self, job_id: str, progress) -> None:
        if self.engine.repository.get(job_id).status is not JobStatus.UPLOADING: return
        states = {OnlineItemStatus.BACKED_UP, OnlineItemStatus.READY, OnlineItemStatus.UPLOADING,
                  OnlineItemStatus.STAGED, OnlineItemStatus.REMOTE_VERIFIED}
        total = self.repository.count_statuses(job_id, states); run = self._run(job_id)
        self._mkdirs(None, run["staging_root"])
        for number, item in enumerate(self.repository.iter_items(job_id, states), 1):
            if not self._may_continue(job_id): return
            candidate = Path(item.candidate_path or "")
            if not candidate.is_file() or self._checksum(candidate) != item.candidate_checksum:
                raise IOError("Validated candidate is missing or changed.")
            suffix = {"WEBP": ".webp", "AVIF": ".avif"}.get(item.candidate_format)
            if not suffix:
                item.status, item.error = OnlineItemStatus.SKIPPED, "Online deployment supports WebP/AVIF sidecars only."
                self.repository.save_item(item); continue
            relative = self._relative(item.remote_path, run["uploads_root"])
            production = safe_join(run["uploads_root"], str(PurePosixPath(relative).with_suffix(suffix)))
            staging = safe_join(run["staging_root"], str(PurePosixPath(relative).with_suffix(suffix)))
            if production == item.remote_path:
                item.status, item.error = OnlineItemStatus.SKIPPED, "Original replacement is deferred; same-format candidate retained locally."
                self.repository.save_item(item); continue
            item.production_path, item.staging_path = production, staging
            self._mkdirs(item, posixpath.dirname(staging))
            if item.status in {OnlineItemStatus.BACKED_UP, OnlineItemStatus.READY, OnlineItemStatus.UPLOADING}:
                item.status = OnlineItemStatus.UPLOADING; self.repository.save_item(item)
                if self.resources:
                    with self.resources.reserve_pool("upload"):
                        self._remote(item, lambda: self.server.upload(candidate, staging))
                else:
                    self._remote(item, lambda: self.server.upload(candidate, staging))
                item.status, item.upload_status = OnlineItemStatus.STAGED, "UPLOADED"
                self.repository.save_item(item)
            if not self._remote_matches(item, staging):
                item.verification_status = "FAILED"; self.repository.save_item(item)
                raise IOError("Remote staging verification failed; production was not touched.")
            item.status, item.verification_status = OnlineItemStatus.REMOTE_VERIFIED, "VERIFIED"
            self.repository.save_item(item); self._checkpoint(job_id, "remote_staging_verified", item)
            if self._remote(item, lambda: self.server.exists(production)):
                if not self._remote_matches(item, production):
                    raise FileExistsError("A different production sidecar already exists; original remains untouched.")
                self._remote(item, lambda: self.server.delete(staging))
            else:
                self._remote(item, lambda: self.server.rename(staging, production))
            if not self._remote_matches(item, production):
                raise IOError("Promoted sidecar failed verification; original remains untouched.")
            item.status = OnlineItemStatus.PROMOTED
            self.repository.save_item(item); self._checkpoint(job_id, "remote_sidecar_promoted", item)
            self._progress(progress, "Uploading / Verifying", number, total, item.remote_path)
        self.engine.transition(job_id, JobStatus.DB_UPDATING)

    def _database_update(self, job_id: str, progress) -> None:
        if self.engine.repository.get(job_id).status is not JobStatus.DB_UPDATING: return
        states = {OnlineItemStatus.PROMOTED, OnlineItemStatus.DB_PENDING}
        total = self.repository.count_statuses(job_id, states); done = 0
        iterator = self.repository.iter_items(job_id, states)
        while batch := tuple(self._take(iterator, 100)):
            changes = tuple(ReferenceChange(i.remote_path, i.production_path) for i in batch if i.production_path)
            self.updater.prepare(changes)
            for item in batch: item.status, item.database_status = OnlineItemStatus.DB_PENDING, "PREPARED"; self.repository.save_item(item)
            self._checkpoint(job_id, "database_update_prepared", payload={"changes": len(changes)})
            self.updater.apply(changes)
            if not self.updater.verify(changes): raise IOError("Database reference verification failed.")
            for item in batch:
                done += 1; item.status, item.database_status = OnlineItemStatus.DB_UPDATED, "UPDATED"
                self.repository.save_item(item); self._progress(progress, "Database update", done, total, item.remote_path)
        self.engine.transition(job_id, JobStatus.VERIFYING)

    def _final_verify(self, job_id: str, progress) -> None:
        if self.engine.repository.get(job_id).status is not JobStatus.VERIFYING: return
        states = {OnlineItemStatus.DB_UPDATED}; total = self.repository.count_statuses(job_id, states)
        for number, item in enumerate(self.repository.iter_items(job_id, states), 1):
            changes = (ReferenceChange(item.remote_path, item.production_path),)
            if not self.updater.verify(changes): raise IOError("Final database verification failed.")
            if not self._remote_matches(item, item.production_path): raise IOError("Final remote verification failed.")
            # The final safety coordinator—not this deployment stage—owns any deletion.
            if not self._remote(item, lambda: self.server.exists(item.remote_path)): raise IOError("Original unexpectedly missing.")
            item.status, item.verification_status = OnlineItemStatus.FINAL_VERIFIED, "FINAL_VERIFIED"
            self.repository.save_item(item); self._checkpoint(job_id, "online_final_verified", item)
            self._progress(progress, "Final verify", number, total, item.remote_path)
        report = self.repository.report(job_id)
        self.engine.update_statistics(job_id, files_total=report.total,
            files_completed=report.completed + report.skipped + report.failed,
            original_bytes=report.original_bytes, optimized_bytes=report.candidate_bytes,
            progress=100 if report.total else 100)
        self.engine.transition(job_id, JobStatus.CLEANUP)

    def _cleanup(self, job_id: str) -> None:
        """Resume final cleanup independently after a crash at its safe boundary."""
        if self.engine.repository.get(job_id).status is not JobStatus.CLEANUP:
            return
        if self.finalizer:
            report = self.finalizer.finalize(job_id)
            self._checkpoint(job_id, "cleanup_complete", payload={"originals_deleted": report.originals_deleted})
        else:
            self._checkpoint(job_id, "cleanup_deferred", payload={"originals_deleted": 0})
        self.engine.transition(job_id, JobStatus.COMPLETED)

    def _remote_matches(self, item: OnlineItem, path: str | None) -> bool:
        if not path or not self._remote(item, lambda: self.server.exists(path)): return False
        stat = self._remote(item, lambda: self.server.stat(path))
        if stat.size is not None and stat.size != item.candidate_bytes: return False
        expected_mime = {"WEBP": "image/webp", "AVIF": "image/avif"}.get(item.candidate_format)
        if stat.mime_type and expected_mime and stat.mime_type.casefold() != expected_mime: return False
        prefix = self._remote(item, lambda: self.server.read_prefix(path, 64))
        if detect_format(prefix) != item.candidate_format: return False
        checksum = self._remote(item, lambda: self.server.checksum(path))
        if checksum: return checksum.casefold() == (item.candidate_checksum or "").casefold()
        verify = self.project_data / "cache" / "remote-verify" / item.job_id / (item.id + ".bin")
        verify.parent.mkdir(parents=True, exist_ok=True)
        self._remote(item, lambda: self.server.download(path, verify))
        try:
            signature = detect_format(verify.read_bytes()[:64])
            return self._checksum(verify) == item.candidate_checksum and signature == item.candidate_format
        finally:
            verify.unlink(missing_ok=True)

    def _remote(self, item: OnlineItem | None, operation):
        last = None; policy = self.engine.retry_policy
        for attempt in range(1, policy.max_attempts + 1):
            try:
                if not self.server.connected: self.server.connect()
                return operation()
            except Exception as exc:
                last = exc; self.server.disconnect()
                if item: item.retry_count += 1; item.error = f"{type(exc).__name__}: {exc}"; self.repository.save_item(item)
                if attempt < policy.max_attempts: self.sleeper(policy.delay_for(attempt))
        raise last

    def _mkdirs(self, item: OnlineItem | None, path: str) -> None:
        current = "/"
        for part in normalize_remote_path(path).strip("/").split("/"):
            if not part: continue
            current = safe_join(current, part)
            if not self._remote(item, lambda p=current: self.server.exists(p)):
                self._remote(item, lambda p=current: self.server.mkdir(p))

    def _checkpoint(self, job_id: str, operation: str, item: OnlineItem | None = None,
                    payload: dict | None = None, safe: bool = True) -> None:
        with self.engine.repository.connection() as connection:
            self.engine.repository.checkpoint(connection, job_id, operation,
                item.status.value if item else self.engine.repository.get(job_id).status.value,
                item.id if item else None, payload=payload, is_safe=safe)

    def _run(self, job_id: str) -> dict:
        run = self.repository.run(job_id)
        if not run: raise KeyError(f"Online run not found: {job_id}")
        return run

    def _may_continue(self, job_id: str) -> bool:
        return self.engine.repository.get(job_id).status not in {
            JobStatus.PAUSED, JobStatus.CANCELLING, JobStatus.CANCELLED, JobStatus.FAILED}

    @staticmethod
    def _relative(path: str, root: str) -> str:
        path, root = normalize_remote_path(path), normalize_remote_path(root)
        if path != root and not path.startswith(root.rstrip("/") + "/"): raise ValueError("Remote item escaped uploads root.")
        return path[len(root):].lstrip("/")

    @staticmethod
    def _checksum(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024): digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _progress(callback, stage: str, done: int, total: int, item: str) -> None:
        if callback: callback(stage, done, total, item)

    @staticmethod
    def _take(iterator, limit: int):
        for _ in range(limit):
            try: yield next(iterator)
            except StopIteration: return
