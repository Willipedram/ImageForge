"""Atomic, checksum-verifiable job backup bundles."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import asdict
from pathlib import Path

from app.database.jobs import JobRepository
from app.database.online import OnlineRepository
from app.database.safety import SafetyRepository


class BackupBundleManager:
    def __init__(self, project_data: Path, jobs: JobRepository,
                 online: OnlineRepository, safety: SafetyRepository) -> None:
        self.project_data, self.jobs, self.online, self.safety = project_data, jobs, online, safety

    def create(self, job_id: str, database_backup: Path) -> Path:
        destination = self.project_data / "backups" / "jobs" / job_id
        if destination.exists():
            self.verify(destination); return destination
        temporary = destination.with_name(destination.name + ".building")
        if temporary.exists(): shutil.rmtree(temporary)
        temporary.mkdir(parents=True)
        job = self.jobs.get(job_id)
        if not job: raise KeyError(job_id)
        database_checksum = _checksum(database_backup)
        manifest = {
            "format": 1, "job": job.to_dict(),
            "images_file": "images.jsonl", "references_file": "reference-changes.jsonl",
            "database_backup": {"path": str(database_backup), "sha256": database_checksum},
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        with (temporary / manifest["images_file"]).open("w", encoding="utf-8") as output:
            for item in self.online.iter_items(job_id):
                output.write(json.dumps(asdict(item), ensure_ascii=False, sort_keys=True) + "\n")
        with (temporary / manifest["references_file"]).open("w", encoding="utf-8") as output:
            self._write_reference_changes(job_id, output)
        # SQLite's backup API captures WAL state consistently, unlike copying the file directly.
        with self.jobs.connection() as source, sqlite3.connect(temporary / "job-state.db") as target:
            source.backup(target)
        checksums = {path.name: _checksum(path) for path in temporary.iterdir() if path.is_file()}
        (temporary / "checksums.json").write_text(json.dumps(checksums, sort_keys=True) + "\n", encoding="utf-8")
        destination.parent.mkdir(parents=True, exist_ok=True); temporary.replace(destination)
        self.verify(destination)
        checksum = _checksum(destination / "manifest.json")
        self.safety.save_bundle(job_id, str(destination), checksum, True)
        return destination

    def verify(self, bundle: Path) -> None:
        checksums = json.loads((bundle / "checksums.json").read_text(encoding="utf-8"))
        for name, expected in checksums.items():
            if _checksum(bundle / name) != expected: raise IOError(f"Backup bundle verification failed for {name}.")
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        database = Path(manifest["database_backup"]["path"])
        if not database.is_file() or _checksum(database) != manifest["database_backup"]["sha256"]:
            raise IOError("The database backup associated with the bundle is missing or changed.")

    def _write_reference_changes(self, job_id: str, output) -> None:
        with self.jobs.connection() as connection:
            cursor = connection.execute(
                "SELECT * FROM database_reference_changes WHERE job_id=? ORDER BY id", (job_id,))
            while rows := cursor.fetchmany(500):
                for row in rows: output.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024): digest.update(chunk)
    return digest.hexdigest()
