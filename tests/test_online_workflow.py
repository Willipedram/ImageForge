from __future__ import annotations

import hashlib
import io
import sqlite3
from pathlib import Path, PurePosixPath

import pytest

from app.core.engine import JobEngine
from app.core.jobs import JobStatus
from app.core.retry import RetryPolicy
from app.database.jobs import DATABASE_SCHEMA_VERSION, JobRepository
from app.database.online import OnlineRepository
from app.image.optimization_models import OptimizationDecision, OptimizationResult
from app.online.models import OnlineItemStatus, ReferenceUpdater
from app.online.workflow import OnlineWorkflow
from app.server.base import RemoteEntry, RemoteServer


class FakeServer(RemoteServer):
    def __init__(self, *, checksums=True):
        self.files = {"/site/wp-config.php": b"config", "/site/index.php": b"index",
                      "/site/wp-content/uploads/2026/photo.jpg": b"original-jpeg"}
        self.directories = {"/", "/site", "/site/wp-admin", "/site/wp-includes", "/site/wp-content",
                            "/site/wp-content/uploads", "/site/wp-content/uploads/2026"}
        self._connected = False; self.checksums = checksums; self.fail_upload_once = False

    @property
    def connected(self): return self._connected
    def connect(self): self._connected = True
    def disconnect(self): self._connected = False
    def list(self, path):
        path = path.rstrip("/") or "/"; found = {}
        for directory in self.directories:
            if directory == path: continue
            parent = str(PurePosixPath(directory).parent)
            if parent == path: found[directory] = RemoteEntry(directory, PurePosixPath(directory).name, True)
        for filename, data in self.files.items():
            if str(PurePosixPath(filename).parent) == path:
                found[filename] = RemoteEntry(filename, PurePosixPath(filename).name, False, len(data))
        return list(found.values())
    def stat(self, path):
        if path in self.directories: return RemoteEntry(path, PurePosixPath(path).name, True)
        data = self.files[path]; return RemoteEntry(path, PurePosixPath(path).name, False, len(data))
    def download(self, remote_path, destination):
        data = self.files[remote_path]
        if hasattr(destination, "write"): destination.write(data)
        else: Path(destination).write_bytes(data)
    def upload(self, source, remote_path):
        if self.fail_upload_once:
            self.fail_upload_once = False; raise ConnectionError("temporary disconnect")
        self.files[remote_path] = Path(source).read_bytes() if isinstance(source, Path) else source.read()
    def delete(self, path): self.files.pop(path, None)
    def rename(self, source, destination): self.files[destination] = self.files.pop(source)
    def exists(self, path): return path in self.files or path in self.directories
    def mkdir(self, path): self.directories.add(path)
    def checksum(self, path, algorithm="sha256"):
        return hashlib.sha256(self.files[path]).hexdigest() if self.checksums and path in self.files else None
    def read_prefix(self, path, maximum_bytes): return self.files[path][:maximum_bytes]


class FakeOptimizer:
    def __init__(self, root): self.root = root; self.calls = 0
    def optimize(self, job_id, source, original_path):
        self.calls += 1; candidate = self.root / job_id / "candidate.webp"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"RIFF\x0c\x00\x00\x00WEBPVP8 " + b"optimized")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        return OptimizationResult(job_id, original_path, source.stat().st_size,
            OptimizationDecision.SELECTED, "WebP selected after quality validation",
            str(candidate), "WEBP", candidate.stat().st_size, checksum=digest,
            validation_passed=True, validation_reason="Passed", confidence="HIGH")


class FakeUpdater(ReferenceUpdater):
    def __init__(self): self.prepared = (); self.applied = (); self.valid = True
    def prepare(self, changes): self.prepared = changes
    def apply(self, changes): self.applied = changes
    def verify(self, changes): return self.valid and all(change in self.applied for change in changes)


@pytest.fixture
def online(tmp_path):
    data = tmp_path / "ProjectData"
    for name in ("jobs", "backups", "cache"): (data / name).mkdir(parents=True)
    jobs = JobRepository(data / "jobs" / "state.db"); jobs.initialize()
    engine = JobEngine(jobs, RetryPolicy(max_attempts=2, base_delay_seconds=.001))
    server, updater = FakeServer(), FakeUpdater()
    workflow = OnlineWorkflow(data, engine, server, FakeOptimizer(data / "generated"), updater,
                              sleeper=lambda _: None)
    return workflow, server, updater, data


def test_full_online_pipeline_preserves_original_and_persists_manifest(online):
    workflow, server, updater, data = online
    job_id = workflow.create("example.test", "/")
    stages = []
    report = workflow.run(job_id, lambda stage, done, total, item: stages.append(stage))
    production = "/site/wp-content/uploads/2026/photo.webp"
    assert server.files["/site/wp-content/uploads/2026/photo.jpg"] == b"original-jpeg"
    assert server.files[production].startswith(b"RIFF")
    assert updater.applied[0].replacement_path == production
    assert report.completed == 1 and report.failed == 0 and report.bytes_uploaded > 0
    assert (data / "backups" / "online" / job_id / "2026" / "photo.jpg").read_bytes() == b"original-jpeg"
    item = next(workflow.manifest(job_id))
    assert item.status is OnlineItemStatus.FINAL_VERIFIED
    assert item.upload_status == "UPLOADED" and item.database_status == "UPDATED"
    assert {"Downloading", "Optimizing", "Uploading / Verifying", "Final verify"} <= set(stages)
    assert workflow.engine.repository.get(job_id).status is JobStatus.COMPLETED


def test_upload_retries_without_touching_original(online):
    workflow, server, _, _ = online; server.fail_upload_once = True
    job_id = workflow.create("retry.test")
    report = workflow.run(job_id)
    assert report.retries == 1
    assert server.files["/site/wp-content/uploads/2026/photo.jpg"] == b"original-jpeg"


def test_verification_without_server_checksum_streams_candidate(online):
    workflow, server, _, _ = online; server.checksums = False
    job_id = workflow.create("no-checksum.test")
    assert workflow.run(job_id).completed == 1


def test_different_existing_sidecar_aborts_before_database_update(online):
    workflow, server, updater, _ = online
    server.files["/site/wp-content/uploads/2026/photo.webp"] = b"someone-elses-file"
    job_id = workflow.create("collision.test")
    with pytest.raises(FileExistsError): workflow.run(job_id)
    assert not updater.applied
    assert server.files["/site/wp-content/uploads/2026/photo.jpg"] == b"original-jpeg"


def test_reconcile_verified_remote_sidecar_after_crash(online):
    workflow, server, _, _ = online
    job_id = workflow.create("recovery.test")
    # Stop immediately after upload/promotion by making DB preparation crash.
    workflow.updater.prepare = lambda changes: (_ for _ in ()).throw(RuntimeError("crash"))
    with pytest.raises(RuntimeError): workflow.run(job_id)
    item = next(workflow.manifest(job_id)); assert item.status is OnlineItemStatus.PROMOTED
    workflow.reconcile(job_id)
    assert next(workflow.manifest(job_id)).status is OnlineItemStatus.PROMOTED
    assert server.files[item.remote_path] == b"original-jpeg"


def test_final_safety_hook_runs_and_cleanup_stage_resumes(online):
    workflow, _, _, _ = online
    calls = []
    class Finalizer:
        def finalize(self, job_id):
            calls.append(job_id)
            return type("Report", (), {"originals_deleted": 0})()
    workflow.finalizer = Finalizer()
    job_id = workflow.create("final-safety.test")
    workflow.run(job_id)
    assert calls == [job_id]
    # A crash after the CLEANUP transition invokes only the idempotent finalizer on resume.
    job = workflow.engine.repository.get(job_id); job.status = JobStatus.CLEANUP
    workflow.engine.repository.save(job); calls.clear()
    workflow.run(job_id)
    assert calls == [job_id] and workflow.engine.repository.get(job_id).status is JobStatus.COMPLETED


def test_schema_migrates_five_to_online_tables(tmp_path):
    database = tmp_path / "v5.db"
    with sqlite3.connect(database) as connection: connection.execute("PRAGMA user_version=5")
    JobRepository(database).initialize()
    with sqlite3.connect(database) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert {"online_runs", "online_items"} <= tables
    assert version == DATABASE_SCHEMA_VERSION == 8
