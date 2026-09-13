"""Explicit backup-retention policy enforcement."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from app.core.jobs import TERMINAL_JOB_STATES
from app.database.jobs import JobRepository
from app.safety.models import BackupRetention


class BackupRetentionService:
    def __init__(self, project_data: Path, jobs: JobRepository) -> None:
        self.project_data, self.jobs = project_data.resolve(), jobs

    def purge(self, policy: BackupRetention, *, now: float | None = None) -> tuple[str, ...]:
        """Delete only terminal-job backups after an explicitly supplied finite policy."""
        if policy.days is None: return ()
        cutoff = (now or time.time()) - policy.days * 86400
        root = self.project_data / "backups" / "jobs"
        removed: list[str] = []
        if not root.is_dir(): return ()
        for directory in root.iterdir():
            if not directory.is_dir() or directory.is_symlink() or directory.stat().st_mtime >= cutoff: continue
            job = self.jobs.get(directory.name)
            if not job or job.status not in TERMINAL_JOB_STATES: continue
            resolved = directory.resolve()
            if self.project_data not in resolved.parents: raise RuntimeError("Backup path escaped ProjectData.")
            shutil.rmtree(resolved)
            for associated in (self.project_data / "backups" / "online" / job.id,
                               self.project_data / "jobs" / job.id / "database"):
                if associated.is_dir() and not associated.is_symlink(): shutil.rmtree(associated)
            removed.append(job.id)
        return tuple(removed)
