from __future__ import annotations

import json
import logging

import pytest

from app.config.settings import AppSettings, ConfigurationStore
from app.core.jobs import Job, JobStatus
from app.core.version import DATA_SCHEMA_VERSION
from app.database.jobs import JobRepository
from app.main import bootstrap, main
from app.storage.project_data import DIRECTORIES, ProjectDataManager
from app.utils.logging import StructuredFormatter, configure_logging


def test_project_data_creation_and_schema(tmp_path):
    root = tmp_path / "durable-data"
    manager = ProjectDataManager(root)
    manager.initialize()
    assert all((root / name).is_dir() for name in DIRECTORIES)
    assert manager.schema_version() == DATA_SCHEMA_VERSION


def test_configuration_load_defaults_and_save(tmp_path):
    store = ConfigurationStore(tmp_path)
    defaults = store.load()
    assert defaults.project_data_path == str(tmp_path.resolve())
    defaults.theme = "dark"
    defaults.max_workers = 8
    defaults.retry_count = 5
    store.save(defaults)
    loaded = store.load()
    assert loaded.theme == "dark"
    assert loaded.max_workers == 8
    assert loaded.retry_count == 5


def test_configuration_rejects_invalid_values(tmp_path):
    store = ConfigurationStore(tmp_path)
    settings = AppSettings.defaults(tmp_path)
    settings.max_workers = 0
    with pytest.raises(ValueError):
        store.save(settings)


def test_job_model_round_trip_and_sqlite_repository(tmp_path):
    job = Job(target="example.com", status=JobStatus.SCANNING, progress=42.5, current_stage="Scanning")
    restored = Job.from_dict(job.to_dict())
    assert restored == job
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    repository.save(job)
    assert repository.get(job.id) == job


def test_logging_human_and_structured(tmp_path):
    path = configure_logging(tmp_path, "DEBUG")
    logging.getLogger("test").warning("visible message")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "visible message" in path.read_text(encoding="utf-8")
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
    assert json.loads(StructuredFormatter().format(record))["message"] == "hello"


def test_bootstrap_and_startup_check(tmp_path):
    settings, store = bootstrap(tmp_path)
    assert store.path.exists()
    assert settings.project_data_path == str(tmp_path.resolve())
    assert (tmp_path / "jobs" / "state.db").exists()
    assert main(["--project-data", str(tmp_path), "--check-startup"]) == 0
