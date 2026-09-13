from __future__ import annotations

import json
import logging
import sqlite3
import sys
import tracemalloc
from uuid import uuid4
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config.settings import ConfigurationStore, default_project_data_path
from app.core.engine import JobEngine
from app.core.jobs import JobStatus
from app.core.runtime_versions import runtime_versions
from app.core.version import APP_VERSION, DATA_SCHEMA_VERSION
from app.database.jobs import DATABASE_SCHEMA_VERSION, JobRepository
from app.database.offline import OfflineRepository
from app.database.online import OnlineRepository
from app.database.wordpress import _quote
from app.server.base import ConnectionConfig, Protocol, RuntimeCredentials
from app.server.credentials import WindowsCredentialProvider
from app.server.ftp import FTPServer
from app.server.preflight import PreflightService
from app.server.resilience import ResilientServer
from app.server.sftp import SFTPServer
from app.core.retry import RetryPolicy
from app.offline.models import LocalItemStatus
from app.offline.workflow import OfflineWorkflow
from app.online.models import OnlineItem
from app.storage.project_data import ProjectDataManager
from app.utils.logging import configure_logging


class CredentialError(Exception):
    winerror = 1168


class CredentialBackend:
    CRED_TYPE_GENERIC = 1; CRED_PERSIST_LOCAL_MACHINE = 2; error = CredentialError
    def __init__(self): self.values = {}
    def CredWrite(self, value, flags): self.values[value["TargetName"]] = value
    def CredRead(self, target, kind):
        if target not in self.values: raise CredentialError()
        return self.values[target]
    def CredDelete(self, target, kind):
        if target not in self.values: raise CredentialError()
        del self.values[target]


def test_windows_credentials_use_secure_backend_and_reject_unsafe_ids():
    backend = CredentialBackend(); provider = WindowsCredentialProvider(backend)
    provider.save("sftp:example.test", RuntimeCredentials("alice", "release-secret"))
    assert "release-secret" not in repr(provider.load("sftp:example.test"))
    assert provider.load("sftp:example.test").password == "release-secret"
    provider.delete("sftp:example.test"); assert provider.load("sftp:example.test") is None
    with pytest.raises(ValueError): provider.save("../escape", RuntimeCredentials("x", "y"))


def test_logs_configuration_and_sqlite_never_persist_keyed_secrets(tmp_path):
    log = configure_logging(tmp_path)
    logging.getLogger("security").error("password=release-secret token=abc123")
    for handler in logging.getLogger().handlers: handler.flush()
    assert "release-secret" not in log.read_text(encoding="utf-8")
    store = ConfigurationStore(tmp_path); settings = store.load(); store.save(settings)
    repository = JobRepository(tmp_path / "jobs" / "state.db"); repository.initialize()
    raw = store.path.read_bytes() + repository.database_path.read_bytes()
    assert b"release-secret" not in raw and b"abc123" not in raw


def test_sql_identifiers_and_project_paths_are_injection_safe(tmp_path):
    assert _quote("custom_posts") == "`custom_posts`"
    with pytest.raises(ValueError): _quote("posts; DROP TABLE users")
    with pytest.raises(ValueError): WindowsCredentialProvider._target("credential/../../escape")


def test_project_data_v1_upgrade_backs_up_and_preserves_jobs(tmp_path):
    root = tmp_path / "external" / "ProjectData"; (root / "jobs").mkdir(parents=True)
    (root / "backups").mkdir(); (root / "logs").mkdir(); (root / "cache").mkdir()
    for name in ("config", "database", "manifests"): (root / name).mkdir()
    (root / "data_schema.json").write_text('{"data_schema_version":1}', encoding="utf-8")
    repository = JobRepository(root / "jobs" / "state.db"); repository.initialize(); engine = JobEngine(repository)
    complete = engine.create_job("completed.example"); complete.status = JobStatus.COMPLETED; repository.save(complete)
    interrupted = engine.create_job("recover.example"); interrupted.status = JobStatus.DOWNLOADING; repository.save(interrupted)
    ProjectDataManager(root).initialize()  # Represents newer source pointed at the old external data.
    assert ProjectDataManager(root).schema_version() == DATA_SCHEMA_VERSION == 2
    assert (root / "config" / "data_policy.json").is_file()
    backups = list((root / "backups").glob("schema-v1-*")); assert len(backups) == 1
    assert (backups[0] / "jobs" / "state.db").is_file()
    visible = {job.id for job in repository.list_jobs()}; assert {complete.id, interrupted.id} <= visible
    reports = engine.recover_interrupted_jobs(); assert any(report.job.id == interrupted.id for report in reports)
    assert repository.get(complete.id).status is JobStatus.COMPLETED


def test_release_versions_and_packaging_exclude_project_data(monkeypatch, tmp_path):
    assert APP_VERSION == "1.0.0" and DATABASE_SCHEMA_VERSION == 9
    versions = runtime_versions(); assert versions["application"] == APP_VERSION and "webp_encoder" in versions
    spec = Path("ImageForge.spec").read_text(encoding="utf-8")
    assert 'name="ImageOptimizer"' in spec and "ProjectData" not in spec
    monkeypatch.setattr(sys, "frozen", True, raising=False); monkeypatch.setattr(sys, "executable", str(tmp_path / "ImageOptimizer.exe"))
    monkeypatch.delenv("IMAGEFORGE_PROJECT_DATA", raising=False)
    assert default_project_data_path() == tmp_path / "ProjectData"


def test_windows_executable_workflow_publishes_click_to_run_artifact():
    workflow = Path(".github/workflows/windows-executable.yml").read_text(encoding="utf-8")
    launcher = Path("build_windows.bat").read_text(encoding="utf-8")
    build_script = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")
    assert "scripts\\build_windows.ps1" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "dist/ImageOptimizer.exe" in workflow
    assert "ImageOptimizer.exe.sha256" in workflow
    assert "--check-startup --project-data" in build_script
    assert "scripts\\build_windows.ps1" in launcher


def test_job_database_v8_migrates_and_records_runtime_versions(tmp_path):
    database = tmp_path / "state.db"
    JobRepository(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE job_runtime_versions")
        connection.execute("PRAGMA user_version=8")
    repository = JobRepository(database)
    repository.initialize()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == DATABASE_SCHEMA_VERSION
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='job_runtime_versions'"
        ).fetchone()
    job = JobEngine(repository).create_job("versioned.example")
    with sqlite3.connect(database) as connection:
        payload = connection.execute(
            "SELECT versions FROM job_runtime_versions WHERE job_id=?", (job.id,)
        ).fetchone()[0]
    assert json.loads(payload)["application"] == APP_VERSION


def test_ftp_and_ftps_connect_without_leaking_password(monkeypatch):
    clients = []
    class Client:
        def __init__(self, *args, **kwargs): clients.append(self); self.logged = None; self.tls = False
        def connect(self, host, port): self.endpoint = (host, port)
        def login(self, user, password): self.logged = (user, password)
        def prot_p(self): self.tls = True
        def quit(self): pass
        def close(self): pass
    monkeypatch.setattr("app.server.ftp.ftplib.FTP", Client)
    monkeypatch.setattr("app.server.ftp.ftplib.FTP_TLS", Client)
    for protocol in (Protocol.FTP, Protocol.FTPS):
        server = FTPServer(ConnectionConfig(protocol, "example.test", 21), RuntimeCredentials("u", "secret"))
        server.connect(); assert server.connected and clients[-1].logged == ("u", "secret")
        if protocol is Protocol.FTPS: assert clients[-1].tls
        assert "secret" not in repr(server._credentials); server.disconnect()


def test_sftp_connects_with_host_key_verification(monkeypatch):
    class SSH:
        def load_system_host_keys(self): self.loaded = True
        def set_missing_host_key_policy(self, policy): self.policy = policy
        def connect(self, *args, **kwargs): self.kwargs = kwargs
        def open_sftp(self): return SimpleNamespace(close=lambda: None)
        def close(self): pass
    ssh = SSH(); fake = SimpleNamespace(SSHClient=lambda: ssh, RejectPolicy=lambda: "reject", WarningPolicy=lambda: "warn",
        AuthenticationException=type("AuthenticationException", (Exception,), {}), SSHException=type("SSHException", (Exception,), {}))
    monkeypatch.setitem(sys.modules, "paramiko", fake)
    server = SFTPServer(ConnectionConfig(Protocol.SFTP, "example.test", 22), RuntimeCredentials("u", "secret"))
    server.connect()
    assert ssh.loaded and ssh.policy == "reject" and ssh.kwargs["look_for_keys"] is False
    assert "secret" not in repr(server._credentials)
    server.disconnect()


def test_timeout_retries_are_bounded_and_insufficient_disk_fails_preflight(monkeypatch, tmp_path):
    class TimeoutServer:
        connected = True
        attempts = 0
        def connect(self): self.connected = True
        def disconnect(self): self.connected = False
        def list(self, path): return []
        def stat(self, path): return SimpleNamespace(path=path)
    server = TimeoutServer()
    def timeout():
        server.attempts += 1
        raise TimeoutError("server timed out")
    delays = []
    resilient = ResilientServer(server, RetryPolicy(max_attempts=3), delays.append)
    with pytest.raises(TimeoutError): resilient.call(timeout)
    assert server.attempts == 3 and len(delays) == 2

    monkeypatch.setattr("app.server.preflight.shutil.disk_usage", lambda path: SimpleNamespace(free=10))
    report = PreflightService(server, tmp_path, minimum_free_bytes=100).run("/")
    disk = next(check for check in report.checks if check.name == "Local disk space")
    assert not disk.passed and not report.passed


def test_encoder_failure_is_persisted_and_original_is_untouched(tmp_path):
    data = tmp_path / "ProjectData"; ProjectDataManager(data).initialize()
    repository = JobRepository(data / "jobs" / "state.db"); repository.initialize(); engine = JobEngine(repository)
    class BrokenEncoder:
        def optimize(self, *args): raise RuntimeError("encoder unavailable")
    workflow = OfflineWorkflow(data, engine, BrokenEncoder(), OfflineRepository(repository))
    folder = tmp_path / "images"; folder.mkdir(); source = folder / "logo.svg"; source.write_text("<svg></svg>", encoding="utf-8")
    before = source.read_bytes(); job_id = workflow.create(folder); workflow.scan(job_id); report = workflow.dry_run(job_id)
    assert source.read_bytes() == before and report.failed == 1
    assert next(workflow.repository.iter_items(job_id, {LocalItemStatus.FAILED})).error.startswith("RuntimeError")


def test_large_inventory_persistence_and_iteration_are_bounded(tmp_path):
    repository = JobRepository(tmp_path / "jobs" / "state.db"); repository.initialize(); engine = JobEngine(repository)
    job = engine.create_job("scale.example"); online = OnlineRepository(repository)
    online.create_run(job.id, "/", f"/optimization-temp/{job.id}")
    tracemalloc.start()
    for start in range(0, 10_000, 200):
        online.save_items(OnlineItem(str(uuid4()), job.id, f"/uploads/image-{index}.jpg", 1024)
                          for index in range(start, start + 200))
    count = sum(1 for _ in online.iter_items(job.id, batch_size=127))
    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
    assert count == 10_000 and peak < 20 * 1024 * 1024
