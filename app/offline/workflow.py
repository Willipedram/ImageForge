"""Crash-resumable scan, dry-run, preview, apply, verify, and report workflow."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL

from app.core.engine import JobEngine
from app.core.jobs import JobStatus
from app.database.offline import OfflineRepository
from app.image.intelligence import detect_format
from app.image.optimization_models import OptimizationDecision
from app.image.optimizer import OfflineOptimizer
from app.offline.models import LocalItem, LocalItemStatus, OfflineReport

LOCAL_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg"})


class SourceChangedError(RuntimeError):
    pass


class OfflineWorkflow:
    def __init__(self, project_data: Path, engine: JobEngine, optimizer: OfflineOptimizer,
                 repository: OfflineRepository | None = None) -> None:
        self.project_data = project_data.resolve()
        self.engine = engine
        self.optimizer = optimizer
        self.repository = repository or OfflineRepository(engine.repository)

    def create(self, folder: Path) -> str:
        root = folder.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(root)
        job = self.engine.create_job(f"local:{os.path.normcase(str(root))}")
        self.repository.create_run(job.id, str(root), dry_run=True)
        self.engine.transition(job.id, JobStatus.PRECHECK)
        return job.id

    def scan(self, job_id: str, batch_size: int = 250) -> int:
        run = self._run(job_id)
        job = self.engine.repository.get(job_id)
        if job.status is JobStatus.PRECHECK:
            self.engine.transition(job_id, JobStatus.SCANNING)
        root = Path(run["local_root"])
        batch: list[LocalItem] = []
        for path in self._scan_paths(root):
            if not self._may_continue(job_id):
                break
            stat = path.stat()
            relative = path.relative_to(root).as_posix()
            identifier = str(uuid5(NAMESPACE_URL, f"{job_id}:{relative}"))
            batch.append(LocalItem(
                identifier, job_id, relative, str(path), stat.st_size,
                self._checksum(path), stat.st_mtime_ns,
            ))
            if len(batch) >= batch_size:
                self.repository.save_items(batch)
                batch.clear()
        self.repository.save_items(batch)
        total = self.repository.count(job_id)
        self.engine.update_statistics(
            job_id, files_total=total, files_completed=0,
            original_bytes=sum(item.original_bytes for item in self.repository.iter_items(job_id)),
            optimized_bytes=0, progress=0,
        )
        self._checkpoint(job_id, "local_scan_complete", {"total": total})
        return total

    def dry_run(self, job_id: str, progress: Callable[[int, int, str], None] | None = None) -> OfflineReport:
        job = self.engine.repository.get(job_id)
        if job.status is JobStatus.SCANNING:
            self.engine.transition(job_id, JobStatus.OPTIMIZING)
        elif job.status in {JobStatus.PAUSED, JobStatus.RECOVERABLE}:
            job = self.engine.resume(job_id)
            if job.status is JobStatus.SCANNING:
                job = self.engine.transition(job_id, JobStatus.OPTIMIZING)
        total = self.repository.count(job_id)
        completed = sum(1 for _ in self.repository.iter_items(
            job_id, {LocalItemStatus.PROPOSED, LocalItemStatus.SKIPPED, LocalItemStatus.FAILED,
                     LocalItemStatus.APPLIED},
        ))
        pending = {LocalItemStatus.SCANNED, LocalItemStatus.OPTIMIZING}
        for item in self.repository.iter_items(job_id, pending):
            if not self._may_continue(job_id):
                break
            if item.status is LocalItemStatus.OPTIMIZING and self._recover_optimization_result(item):
                completed += 1
                self._update_progress(job_id, completed, total, item)
                if progress:
                    progress(completed, total, item.relative_path)
                continue
            item.status = LocalItemStatus.OPTIMIZING
            self.repository.save_item(item)
            try:
                result = self.optimizer.optimize(job_id, Path(item.source_path), item.relative_path)
                item.decision_reason = result.decision_reason
                item.width, item.height = result.width, result.height
                if result.decision is OptimizationDecision.SELECTED and result.candidate_path:
                    item.status = LocalItemStatus.PROPOSED
                    item.candidate_path = result.candidate_path
                    item.candidate_format = result.candidate_format
                    item.candidate_bytes = result.candidate_bytes
                    item.candidate_checksum = result.checksum
                elif result.decision is OptimizationDecision.FAILED:
                    item.status = LocalItemStatus.FAILED
                    item.error = result.decision_reason
                else:
                    item.status = LocalItemStatus.SKIPPED
            except Exception as exc:
                item.status = LocalItemStatus.FAILED
                item.error = f"{type(exc).__name__}: {exc}"
            self.repository.save_item(item)
            completed += 1
            self._update_progress(job_id, completed, total, item)
            if progress:
                progress(completed, total, item.relative_path)
        job = self.engine.repository.get(job_id)
        if job.status is JobStatus.OPTIMIZING and completed >= total:
            self.engine.transition(job_id, JobStatus.VALIDATING)
            self.engine.transition(job_id, JobStatus.PREVIEW)
            self._checkpoint(job_id, "dry_run_complete", {"files": total})
        return self.report(job_id)

    def apply(self, job_id: str, *, confirmed: bool,
              progress: Callable[[int, int, str], None] | None = None) -> OfflineReport:
        if not confirmed:
            raise PermissionError("Apply requires explicit user confirmation.")
        job = self.engine.repository.get(job_id)
        if job.status is JobStatus.PREVIEW:
            self.engine.transition(job_id, JobStatus.APPLYING)
        elif job.status in {JobStatus.PAUSED, JobStatus.RECOVERABLE}:
            self.recover(job_id)
            job = self.engine.repository.get(job_id)
            if job.status in {JobStatus.PAUSED, JobStatus.RECOVERABLE}:
                job = self.engine.resume(job_id)
            if job.status is JobStatus.PREVIEW:
                self.engine.transition(job_id, JobStatus.APPLYING)
        elif job.status is not JobStatus.APPLYING:
            raise RuntimeError("Job is not ready to apply.")
        self.repository.set_dry_run(job_id, False)
        candidates = {LocalItemStatus.PROPOSED, LocalItemStatus.APPLYING, LocalItemStatus.BACKED_UP,
                      LocalItemStatus.STAGED, LocalItemStatus.REPLACED}
        total = self.repository.count_statuses(job_id, candidates)
        done = 0
        for item in self.repository.iter_items(job_id, candidates):
            if not self._may_continue(job_id):
                break
            try:
                self._apply_item(item)
            except Exception as exc:
                item.status = LocalItemStatus.FAILED
                item.error = f"{type(exc).__name__}: {exc}"
                self.repository.save_item(item)
                self._checkpoint(job_id, "local_apply_failed", {"item": item.id, "error": item.error}, safe=False)
            done += 1
            if progress:
                progress(done, total, item.relative_path)
        job = self.engine.repository.get(job_id)
        remaining = next(self.repository.iter_items(job_id, candidates), None)
        if job.status is JobStatus.APPLYING and remaining is None:
            self.engine.transition(job_id, JobStatus.VERIFYING)
            self.engine.transition(job_id, JobStatus.CLEANUP)
            self.engine.transition(job_id, JobStatus.COMPLETED)
        return self.report(job_id)

    def recover(self, job_id: str) -> None:
        """Reconcile filesystem side effects before resuming incomplete items."""
        for item in self.repository.iter_items(job_id, {
            LocalItemStatus.APPLYING, LocalItemStatus.BACKED_UP, LocalItemStatus.STAGED,
            LocalItemStatus.REPLACED,
        }):
            candidate_ok = bool(item.target_path and Path(item.target_path).is_file() and
                                self._checksum(Path(item.target_path)) == item.candidate_checksum)
            source = Path(item.source_path)
            target = Path(item.target_path) if item.target_path else None
            if candidate_ok:
                if target != source and source.exists():
                    source.unlink()
                item.status = LocalItemStatus.APPLIED
                self.repository.save_item(item)
                self._checkpoint(job_id, "local_item_reconciled", {"item": item.id, "outcome": "applied"})
                continue
            temporary = self._temporary_path(target, job_id) if target else None
            if temporary and temporary.exists() and self._checksum(temporary) == item.candidate_checksum:
                os.replace(temporary, target)
                item.status = LocalItemStatus.REPLACED
                self.repository.save_item(item)
                continue
            if source.is_file() and self._checksum(source) == item.original_checksum:
                backup = Path(item.backup_path) if item.backup_path else None
                if backup and backup.exists() and self._checksum(backup) != item.original_checksum:
                    backup.unlink(missing_ok=True)
                item.status = LocalItemStatus.PROPOSED
                self.repository.save_item(item)
                continue
            if item.backup_path and Path(item.backup_path).exists():
                self._restore_backup(item)
            item.status = LocalItemStatus.PROPOSED
            self.repository.save_item(item)

    def pause(self, job_id: str) -> None:
        self.engine.pause(job_id)

    def resume(self, job_id: str,
               progress: Callable[[int, int, str], None] | None = None) -> OfflineReport:
        job = self.engine.repository.get(job_id)
        previous = job.resume_state
        self.recover(job_id)
        self.engine.resume(job_id)
        if previous is JobStatus.APPLYING:
            return self.apply(job_id, confirmed=True, progress=progress)
        if previous is JobStatus.SCANNING:
            self.scan(job_id)
        return self.dry_run(job_id, progress=progress)

    def cancel(self, job_id: str) -> None:
        self.engine.cancel(job_id)

    def report(self, job_id: str) -> OfflineReport:
        job = self.engine.repository.get(job_id)
        duration = job.duration_seconds
        if not duration and job.started_at:
            from datetime import datetime, UTC
            duration = (datetime.now(UTC) - datetime.fromisoformat(job.started_at)).total_seconds()
        return self.repository.report(job_id, max(0, duration))

    def previews(self, job_id: str) -> Iterator[LocalItem]:
        return self.repository.iter_items(job_id, {
            LocalItemStatus.PROPOSED, LocalItemStatus.SKIPPED, LocalItemStatus.FAILED,
            LocalItemStatus.APPLIED,
        })

    def _apply_item(self, item: LocalItem) -> None:
        source = Path(item.source_path)
        candidate = Path(item.candidate_path or "")
        if not candidate.is_file() or self._checksum(candidate) != item.candidate_checksum:
            raise FileNotFoundError("Validated candidate is missing or changed.")
        if item.status is LocalItemStatus.REPLACED and item.target_path:
            target = Path(item.target_path)
            if target.is_file() and self._checksum(target) == item.candidate_checksum:
                if target != source and source.exists():
                    source.unlink()
                item.status = LocalItemStatus.APPLIED
                self.repository.save_item(item)
                self._checkpoint(item.job_id, "local_apply_verified", {"item": item.id, "target": str(target)})
                return
        if source.exists():
            stat = source.stat()
            if stat.st_mtime_ns != item.modified_ns or self._checksum(source) != item.original_checksum:
                raise SourceChangedError("Original changed after the dry run.")
        elif item.status not in {LocalItemStatus.REPLACED, LocalItemStatus.STAGED}:
            raise SourceChangedError("Original is missing.")
        extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "AVIF": ".avif", "SVG": ".svg"}[item.candidate_format]
        target = source.with_suffix(extension)
        if target != source and target.exists():
            raise FileExistsError(f"Destination already exists: {target.name}")
        backup = self.project_data / "backups" / "offline" / item.job_id / item.relative_path
        backup.parent.mkdir(parents=True, exist_ok=True)
        item.target_path, item.backup_path = str(target), str(backup)
        item.status = LocalItemStatus.APPLYING
        self.repository.save_item(item)
        if not backup.exists():
            shutil.copy2(source, backup)
        if self._checksum(backup) != item.original_checksum:
            raise IOError("Backup verification failed.")
        item.status = LocalItemStatus.BACKED_UP
        self.repository.save_item(item)
        self._checkpoint(item.job_id, "local_backup_verified", {"item": item.id, "backup": str(backup)})

        temporary = self._temporary_path(target, item.job_id)
        shutil.copyfile(candidate, temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        if self._checksum(temporary) != item.candidate_checksum:
            temporary.unlink(missing_ok=True)
            raise IOError("Staged candidate verification failed.")
        item.status = LocalItemStatus.STAGED
        self.repository.save_item(item)
        self._checkpoint(item.job_id, "local_candidate_staged", {"item": item.id})
        try:
            os.replace(temporary, target)
            item.status = LocalItemStatus.REPLACED
            self.repository.save_item(item)
            if self._checksum(target) != item.candidate_checksum:
                raise IOError("Final checksum verification failed.")
            with target.open("rb") as stream:
                if detect_format(stream.read(64)) != item.candidate_format:
                    raise IOError("Final format verification failed.")
            if target != source:
                source.unlink()
            item.status = LocalItemStatus.APPLIED
            self.repository.save_item(item)
            self._checkpoint(item.job_id, "local_apply_verified", {"item": item.id, "target": str(target)})
        except Exception:
            self._restore_backup(item)
            raise

    def _recover_optimization_result(self, item: LocalItem) -> bool:
        results = getattr(self.optimizer, "repository", None)
        if results is None:
            return False
        result = results.get(item.job_id, item.relative_path)
        if result is None:
            return False
        item.decision_reason = result.decision_reason
        item.width, item.height = result.width, result.height
        if result.decision is OptimizationDecision.SELECTED and result.candidate_path:
            candidate = Path(result.candidate_path)
            if not candidate.is_file() or self._checksum(candidate) != result.checksum:
                return False
            item.status = LocalItemStatus.PROPOSED
            item.candidate_path, item.candidate_format = result.candidate_path, result.candidate_format
            item.candidate_bytes, item.candidate_checksum = result.candidate_bytes, result.checksum
        elif result.decision is OptimizationDecision.FAILED:
            item.status, item.error = LocalItemStatus.FAILED, result.decision_reason
        else:
            item.status = LocalItemStatus.SKIPPED
        self.repository.save_item(item)
        self._checkpoint(item.job_id, "local_optimization_reconciled", {"item": item.id})
        return True

    def _restore_backup(self, item: LocalItem) -> None:
        backup, source = Path(item.backup_path or ""), Path(item.source_path)
        if not backup.is_file():
            raise IOError("Verified backup is unavailable for recovery.")
        temporary = source.with_name(f".{source.name}.imageforge-restore.tmp")
        shutil.copyfile(backup, temporary)
        if self._checksum(temporary) != item.original_checksum:
            temporary.unlink(missing_ok=True)
            raise IOError("Backup restore verification failed.")
        os.replace(temporary, source)
        target = Path(item.target_path) if item.target_path else source
        if target != source:
            target.unlink(missing_ok=True)

    def _update_progress(self, job_id: str, completed: int, total: int, item: LocalItem) -> None:
        original, proposed = self.repository.byte_totals(job_id)
        self.engine.update_statistics(
            job_id, files_total=total, files_completed=completed,
            original_bytes=original, optimized_bytes=proposed,
            progress=completed / total * 100 if total else 100,
        )
        self._checkpoint(job_id, "local_item_processed", {"item": item.id, "status": item.status.value})

    def _checkpoint(self, job_id: str, operation: str, payload: dict, safe: bool = True) -> None:
        state = self.engine.repository.get(job_id).status.value
        with self.engine.repository.connection() as connection:
            self.engine.repository.checkpoint(
                connection, job_id, operation, state,
                payload=payload, is_safe=safe,
            )

    def _run(self, job_id: str) -> dict:
        run = self.repository.run(job_id)
        if not run:
            raise KeyError(f"Offline run not found: {job_id}")
        return run

    def _may_continue(self, job_id: str) -> bool:
        status = self.engine.repository.get(job_id).status
        return status not in {JobStatus.PAUSED, JobStatus.CANCELLING, JobStatus.CANCELLED, JobStatus.FAILED}

    @staticmethod
    def _scan_paths(root: Path) -> Iterator[Path]:
        for directory, names, files in os.walk(root, topdown=True, followlinks=False):
            base = Path(directory)
            names[:] = sorted(name for name in names if not (base / name).is_symlink())
            for name in sorted(files):
                path = base / name
                if not path.is_symlink() and path.suffix.casefold() in LOCAL_EXTENSIONS:
                    yield path

    @staticmethod
    def _checksum(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _temporary_path(target: Path, job_id: str) -> Path:
        return target.with_name(f".{target.name}.imageforge-{job_id[:8]}.tmp")
