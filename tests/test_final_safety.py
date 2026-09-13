from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import struct
import time
import zlib
from pathlib import Path, PurePosixPath

from app.core.engine import JobEngine
from app.core.jobs import JobStatus
from app.database.jobs import DATABASE_SCHEMA_VERSION, JobRepository
from app.database.online import OnlineRepository
from app.database.optimization import OptimizationRepository
from app.image.optimization_models import OptimizationDecision, OptimizationResult
from app.online.models import OnlineItem, OnlineItemStatus
from app.safety.coordinator import FinalSafetyCoordinator
from app.safety.http import HTTPVerifier
from app.safety.models import BackupRetention, HTTPResponse
from app.safety.retention import BackupRetentionService
from app.server.base import RemoteEntry, RemoteServer


def png(width=2, height=2):
    def chunk(kind, data): return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    rows = b"".join(b"\0" + b"\xff\0\0" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


class Server(RemoteServer):
    def __init__(self, files): self.files = dict(files); self.dirs = {"/", "/stage"}; self._connected = False; self.fail_delete = False
    @property
    def connected(self): return self._connected
    def connect(self): self._connected = True
    def disconnect(self): self._connected = False
    def list(self, path): return []
    def stat(self, path): return RemoteEntry(path, PurePosixPath(path).name, False, len(self.files[path]), mime_type="image/png")
    def download(self, remote_path, destination): Path(destination).write_bytes(self.files[remote_path])
    def upload(self, source, remote_path): self.files[remote_path] = Path(source).read_bytes()
    def delete(self, path):
        if self.fail_delete: raise OSError("cleanup unavailable")
        self.files.pop(path, None)
    def rename(self, source, destination): self.files[destination] = self.files.pop(source)
    def exists(self, path): return path in self.files or path in self.dirs
    def mkdir(self, path): self.dirs.add(path)
    def checksum(self, path, algorithm="sha256"): return hashlib.sha256(self.files[path]).hexdigest() if path in self.files else None
    def read_prefix(self, path, maximum_bytes): return self.files[path][:maximum_bytes]


class Updater:
    def __init__(self, backup): self.backup_path = backup; self.refs = False; self.restored = False
    def verify(self, changes): return not self.refs
    def references_exist(self, paths): return self.refs
    def restore_database(self): self.restored = self.refs = True


def database_backup(data, job_id):
    directory = data / "jobs" / job_id / "database"; directory.mkdir(parents=True)
    path = directory / "wordpress-full-backup.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as stream: stream.write(json.dumps({"format": 1, "tables": []}) + "\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest(); path.with_suffix(path.suffix + ".sha256").write_text(digest + "\n")
    return path


def make_safety(tmp_path, *, http_ok=True, cleanup_fails=False):
    data = tmp_path / "ProjectData"; jobs = JobRepository(data / "jobs" / "state.db"); jobs.initialize()
    engine = JobEngine(jobs); job = engine.create_job("remote:safe.test")
    job.status = JobStatus.CLEANUP; jobs.save(job)
    online = OnlineRepository(jobs); online.create_run(job.id, "/site", "/stage"); online.set_discovery(job.id, "/site", "/site/uploads")
    original, candidate = b"original jpeg", png(); original_path, candidate_path = "/site/uploads/photo.jpg", "/site/uploads/photo.png"
    item = OnlineItem("item", job.id, original_path, len(original), OnlineItemStatus.FINAL_VERIFIED,
        hashlib.sha256(original).hexdigest(), candidate_format="PNG", candidate_bytes=len(candidate),
        candidate_checksum=hashlib.sha256(candidate).hexdigest(), production_path=candidate_path,
        decision="SELECTED", upload_status="UPLOADED", verification_status="FINAL_VERIFIED", database_status="UPDATED")
    item.local_checksum = hashlib.sha256(original).hexdigest(); online.save_item(item)
    backup = data / "backups" / "online" / job.id / "photo.jpg"; backup.parent.mkdir(parents=True); backup.write_bytes(original)
    OptimizationRepository(jobs).save(OptimizationResult(job.id, original_path, len(original), OptimizationDecision.SELECTED,
        "selected", candidate_format="PNG", candidate_bytes=len(candidate), width=2, height=2,
        checksum=item.candidate_checksum, validation_passed=True))
    db = database_backup(data, job.id); updater = Updater(db); server = Server({original_path: original, candidate_path: candidate})
    server.fail_delete = cleanup_fails
    response = HTTPResponse(200 if http_ok else 503, {"Content-Type": "image/png", "Server": "cloudflare"}, candidate, "https://site/photo.png")
    coordinator = FinalSafetyCoordinator(data, engine, online, server, updater, lambda path: "https://site/photo.png",
                                         HTTPVerifier(lambda url, limit: response))
    return coordinator, server, updater, item, data


def test_all_gates_pass_before_original_deletion_and_bundle_is_verified(tmp_path):
    coordinator, server, _, item, data = make_safety(tmp_path)
    report = coordinator.finalize(item.job_id)
    assert item.remote_path not in server.files and item.production_path in server.files
    assert report.originals_deleted == 1 and report.originals_retained == 0
    assert "Cloudflare" in report.warnings[0]
    bundle = data / "backups" / "jobs" / item.job_id
    coordinator.bundles.verify(bundle)
    audit = coordinator.safety.audit(item.job_id, item.id)
    assert audit.deletion_authorized and audit.original_deleted and all(audit.checks.values())


def test_http_failure_keeps_original(tmp_path):
    coordinator, server, _, item, _ = make_safety(tmp_path, http_ok=False)
    report = coordinator.finalize(item.job_id)
    assert item.remote_path in server.files and report.originals_deleted == 0
    assert "final http verification" in " ".join(report.verification_errors).casefold()


def test_cleanup_failure_is_warning_not_image_failure(tmp_path):
    coordinator, server, _, item, _ = make_safety(tmp_path, cleanup_fails=True)
    report = coordinator.finalize(item.job_id)
    assert item.remote_path in server.files and report.failed == 0 and report.warnings
    assert coordinator.safety.audit(item.job_id, item.id).deletion_authorized


def test_crash_after_delete_reconciles_from_committed_authorization(tmp_path):
    coordinator, server, _, item, _ = make_safety(tmp_path)
    coordinator.finalize(item.job_id)
    audit = coordinator.safety.audit(item.job_id, item.id)
    coordinator.safety.save_audit(type(audit)(audit.job_id, audit.item_id, audit.original_path, audit.candidate_path,
        audit.checks, True, False, audit.warnings, audit.errors))
    assert item.remote_path not in server.files
    assert coordinator.finalize(item.job_id).originals_deleted == 1


def test_rollback_by_job_restores_original_then_database_and_removes_candidate(tmp_path):
    coordinator, server, updater, item, _ = make_safety(tmp_path)
    coordinator.finalize(item.job_id)
    report = coordinator.rollback(item.job_id)
    assert report.originals_restored == 1 and report.database_restored
    assert item.remote_path in server.files and item.production_path not in server.files and updater.restored


def test_retention_never_and_explicit_age_policy(tmp_path):
    coordinator, _, _, item, data = make_safety(tmp_path); coordinator.finalize(item.job_id)
    job = coordinator.engine.repository.get(item.job_id); job.status = JobStatus.COMPLETED
    coordinator.engine.repository.save(job)
    service = BackupRetentionService(data, coordinator.engine.repository)
    bundle = data / "backups" / "jobs" / item.job_id
    old = time.time() - 100 * 86400
    for path in (bundle,): Path(path).touch(); __import__("os").utime(path, (old, old))
    assert service.purge(BackupRetention.NEVER, now=time.time()) == () and bundle.exists()
    assert service.purge(BackupRetention.NINETY_DAYS, now=time.time()) == (item.job_id,) and not bundle.exists()


def test_schema_migrates_seven_to_safety_tables(tmp_path):
    path = tmp_path / "v7.db"
    with sqlite3.connect(path) as connection: connection.execute("PRAGMA user_version=7")
    JobRepository(path).initialize()
    with sqlite3.connect(path) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert {"backup_bundles", "safety_audits", "rollback_events"} <= tables
    assert version == DATABASE_SCHEMA_VERSION == 9
