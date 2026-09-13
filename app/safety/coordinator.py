"""Final audit, guarded original cleanup, and job-ID rollback."""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Callable

from app.core.engine import JobEngine
from app.core.jobs import JobStatus
from app.database.online import OnlineRepository
from app.database.optimization import OptimizationRepository
from app.database.safety import SafetyRepository
from app.image.intelligence import FORMAT_MIMES, ImageAnalyzer
from app.online.models import OnlineItem, OnlineItemStatus, ReferenceChange
from app.safety.bundle import BackupBundleManager
from app.safety.http import HTTPVerifier
from app.safety.models import FinalReport, RollbackReport, SafetyAudit
from app.server.base import RemoteServer
from app.server.paths import safe_join


class FinalSafetyCoordinator:
    def __init__(self, project_data: Path, engine: JobEngine, online: OnlineRepository,
                 server: RemoteServer, updater, url_for: Callable[[str], str],
                 http: HTTPVerifier | None = None) -> None:
        self.project_data, self.engine, self.online, self.server = project_data, engine, online, server
        self.updater, self.url_for, self.http = updater, url_for, http or HTTPVerifier()
        self.safety = SafetyRepository(engine.repository)
        self.optimizations = OptimizationRepository(engine.repository)
        self.bundles = BackupBundleManager(project_data, engine.repository, online, self.safety)

    def finalize(self, job_id: str) -> FinalReport:
        """Delete an original only after every independently persisted gate passes."""
        self._connect()
        database_backup = self._database_backup(job_id)
        bundle = self.bundles.create(job_id, database_backup)
        self._checkpoint(job_id, "final_audit_started", {"bundle": str(bundle)})
        for item in self.online.iter_items(job_id, {OnlineItemStatus.FINAL_VERIFIED}):
            existing = self.safety.audit(job_id, item.id)
            if existing and existing.original_deleted:
                continue
            audit = self._audit_item(item, bundle)
            self.safety.save_audit(audit)
            if not audit.deletion_authorized:
                continue
            # Authorization and all evidence are committed before the destructive call.
            self._checkpoint(job_id, "original_cleanup_authorized", {"item": item.id})
            try:
                if self.server.exists(item.remote_path): self.server.delete(item.remote_path)
                deleted = not self.server.exists(item.remote_path)
                warnings = audit.warnings if deleted else (*audit.warnings, "Original deletion could not be confirmed.")
                self.safety.save_audit(replace(audit, original_deleted=deleted, warnings=warnings))
            except Exception as exc:
                # Production points at a verified candidate, so cleanup is a warning—not image failure.
                self.safety.save_audit(replace(audit,
                    warnings=(*audit.warnings, f"Cleanup warning: {type(exc).__name__}: {exc}")))
        self._checkpoint(job_id, "final_audit_complete", {"originals_deleted": self._deleted(job_id)})
        return self.report(job_id)

    def rollback(self, job_id: str) -> RollbackReport:
        """Restore originals first, then the database, before removing candidates."""
        bundle_row = self.safety.bundle(job_id)
        if not bundle_row or not bundle_row["verified"]: raise RuntimeError("A verified backup bundle is required.")
        bundle = Path(bundle_row["bundle_path"]); self.bundles.verify(bundle); self._connect()
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        restored = removed = 0; warnings: list[str] = []
        run = self.online.run(job_id)
        for data in self._bundle_images(bundle, manifest):
            if not data.get("production_path"): continue
            original = data["remote_path"]
            backup = self.project_data / "backups" / "online" / job_id / self._relative(original, run["uploads_root"])
            if not backup.is_file() or _checksum(backup) != data["local_checksum"]:
                raise IOError(f"Verified original backup is unavailable for {original}.")
            if not self.server.exists(original):
                temporary = safe_join(run["staging_root"], "rollback", self._relative(original, run["uploads_root"]))
                self._mkdirs(posixpath.dirname(temporary)); self.server.upload(backup, temporary)
                if not self._remote_checksum_matches(temporary, data["local_checksum"], item_id=data["id"]):
                    raise IOError("Rollback staging checksum failed.")
                self.server.rename(temporary, original)
            if not self._remote_checksum_matches(original, data["local_checksum"], item_id=data["id"]):
                raise IOError("Restored original checksum failed.")
            restored += 1
        if hasattr(self.updater, "backup_path"):
            self.updater.backup_path = Path(manifest["database_backup"]["path"])
        self.updater.restore_database()
        paths = (d["remote_path"] for d in self._bundle_images(bundle, manifest) if d.get("production_path"))
        while batch := tuple(_take(paths, 200)):
            if not self.updater.references_exist(batch):
                raise IOError("Rollback database verification did not find restored original references.")
        for data in self._bundle_images(bundle, manifest):
            candidate = data.get("production_path")
            if candidate and self.server.exists(candidate):
                try: self.server.delete(candidate); removed += int(not self.server.exists(candidate))
                except Exception as exc: warnings.append(f"Candidate cleanup warning: {exc}")
        self.safety.rollback_event(job_id, "COMPLETED", f"Restored {restored} originals and database backup")
        return RollbackReport(job_id, restored, removed, True, tuple(warnings))

    def report(self, job_id: str) -> FinalReport:
        base = self.online.report(job_id); audits = self.safety.audits(job_id)
        errors = tuple(error for audit in audits for error in audit.errors)
        warnings = tuple(warning for audit in audits for warning in audit.warnings)
        deleted = sum(a.original_deleted for a in audits)
        job = self.engine.repository.get(job_id)
        audit_by_item = {audit.item_id: audit for audit in audits}
        savings = sum(max(0, item.original_bytes - (item.candidate_bytes or item.original_bytes))
                      for item in self.online.iter_items(job_id)
                      if audit_by_item.get(item.id) and audit_by_item[item.id].original_deleted)
        return FinalReport(job_id, base.completed, base.skipped, base.failed, deleted,
            max(0, base.completed - deleted), self.safety.database_updates(job_id), errors, warnings,
            savings, job.duration_seconds if job else 0.0)

    def _audit_item(self, item: OnlineItem, bundle: Path) -> SafetyAudit:
        errors: list[str] = []; warnings: list[str] = []
        optimization = self.optimizations.get(item.job_id, item.remote_path)
        expected_dimensions = (optimization.width, optimization.height) if optimization else (None, None)
        candidate_exists = bool(item.production_path and self.server.exists(item.production_path))
        remote_readable = remote_mime = dimensions = checksum = False
        if candidate_exists:
            stat = self.server.stat(item.production_path)
            verify_path = self.project_data / "jobs" / item.job_id / "verification" / f"{item.id}.image"
            verify_path.parent.mkdir(parents=True, exist_ok=True); self.server.download(item.production_path, verify_path)
            try:
                record = ImageAnalyzer().analyze(verify_path, job_id=item.job_id, remote_path=item.production_path)
                remote_readable = record.signature_valid and record.detected_format == item.candidate_format
                remote_mime = record.mime_type == FORMAT_MIMES.get(item.candidate_format) and (
                    not stat.mime_type or stat.mime_type.split(";", 1)[0].casefold() == record.mime_type)
                dimensions = all(expected_dimensions) and (record.width, record.height) == expected_dimensions
                checksum = _checksum(verify_path) == item.candidate_checksum
            finally: verify_path.unlink(missing_ok=True)
        change = (ReferenceChange(item.remote_path, item.production_path),)
        references_updated = item.database_status == "UPDATED" and self.updater.verify(change)
        no_old_references = not self.updater.references_exist((item.remote_path,))
        run = self.online.run(item.job_id)
        backup = self.project_data / "backups" / "online" / item.job_id / self._relative(item.remote_path, run["uploads_root"])
        backup_ok = backup.is_file() and _checksum(backup) == item.local_checksum
        bundle_ok = bool(self.safety.bundle(item.job_id))
        self._checkpoint(item.job_id, "item_final_audit_evidence", {"item": item.id})
        checkpoint = self.engine.repository.last_safe_checkpoint(item.job_id)
        state_committed = bool(checkpoint and checkpoint["operation"] == "item_final_audit_evidence")
        http = self.http.verify(self.url_for(item.production_path), item.candidate_format, expected_dimensions)
        if http.cache_layers: warnings.append("Cache/CDN observed: " + ", ".join(http.cache_layers) + "; no purge was attempted.")
        errors.extend(http.errors)
        checks = {"candidate_exists": candidate_exists, "candidate_readable": remote_readable,
            "candidate_mime": remote_mime, "candidate_dimensions": dimensions,
            "candidate_checksum": checksum, "remote_verification": item.verification_status == "FINAL_VERIFIED",
            "database_references": references_updated, "wordpress_metadata": references_updated,
            "no_original_references": no_old_references, "original_backup": backup_ok,
            "backup_bundle": bundle_ok, "job_state_committed": state_committed,
            "final_http_verification": http.passed}
        for name, passed in checks.items():
            if not passed: errors.append(f"Safety gate failed: {name.replace('_', ' ')}.")
        return SafetyAudit(item.job_id, item.id, item.remote_path, item.production_path,
                           checks, all(checks.values()), False, tuple(warnings), tuple(dict.fromkeys(errors)))

    def _database_backup(self, job_id: str) -> Path:
        path = self.project_data / "jobs" / job_id / "database" / "wordpress-full-backup.jsonl.gz"
        if not path.is_file(): raise IOError("Verified database backup is missing.")
        from app.database.wordpress import LogicalDatabaseBackup
        LogicalDatabaseBackup.verify(path); return path

    def _checkpoint(self, job_id: str, operation: str, payload: dict) -> None:
        with self.engine.repository.connection() as connection:
            self.engine.repository.checkpoint(connection, job_id, operation, JobStatus.CLEANUP.value, payload=payload)

    def _connect(self) -> None:
        if not self.server.connected: self.server.connect()

    def _mkdirs(self, path: str) -> None:
        current = "/"
        for part in PurePosixPath(path).parts:
            if part == "/": continue
            current = safe_join(current, part)
            if not self.server.exists(current): self.server.mkdir(current)

    @staticmethod
    def _relative(path: str, root: str) -> str:
        return path.removeprefix(root.rstrip("/") + "/")

    def _deleted(self, job_id: str) -> int:
        return sum(a.original_deleted for a in self.safety.audits(job_id))

    @staticmethod
    def _bundle_images(bundle: Path, manifest: dict):
        with (bundle / manifest["images_file"]).open("r", encoding="utf-8") as stream:
            for line in stream: yield json.loads(line)

    def _remote_checksum_matches(self, path: str, expected: str, *, item_id: str) -> bool:
        checksum = self.server.checksum(path)
        if checksum is not None: return checksum.casefold() == expected.casefold()
        verification = self.project_data / "cache" / "rollback" / f"{item_id}.verify"
        verification.parent.mkdir(parents=True, exist_ok=True); self.server.download(path, verification)
        try: return _checksum(verification) == expected
        finally: verification.unlink(missing_ok=True)


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024): digest.update(chunk)
    return digest.hexdigest()


def _take(iterator, limit):
    for _ in range(limit):
        try: yield next(iterator)
        except StopIteration: return
