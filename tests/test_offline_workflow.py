from __future__ import annotations

import hashlib
import sqlite3
import shutil
from pathlib import Path

import pytest

from app.core.engine import JobEngine
from app.core.jobs import JobStatus
from app.database.jobs import DATABASE_SCHEMA_VERSION, JobRepository
from app.database.offline import OfflineRepository
from app.image.optimization_models import OptimizationDecision, OptimizationResult
from app.offline.models import LocalItemStatus
from app.offline.workflow import OfflineWorkflow, SourceChangedError


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StubOptimizer:
    def __init__(self, project_data: Path):
        self.project_data = project_data
        self.calls = []

    def optimize(self, job_id: str, source: Path, original_path: str) -> OptimizationResult:
        self.calls.append(original_path)
        output = self.project_data / "jobs" / job_id / "processed" / (source.stem + ".svg")
        output.parent.mkdir(parents=True, exist_ok=True)
        data = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><path d="M0 0"/></svg>'
        output.write_bytes(data)
        return OptimizationResult(
            job_id, original_path, source.stat().st_size, OptimizationDecision.SELECTED,
            "SVG selected: validated safe local candidate.", str(output), "SVG", len(data),
            width=10, height=10, checksum=hashlib.sha256(data).hexdigest(), validation_passed=True,
            confidence="HIGH", savings_bytes=max(0, source.stat().st_size - len(data)),
        )


@pytest.fixture
def workflow(tmp_path):
    project_data = tmp_path / "ProjectData"
    jobs = JobRepository(project_data / "jobs" / "state.db")
    jobs.initialize()
    engine = JobEngine(jobs)
    optimizer = StubOptimizer(project_data)
    return OfflineWorkflow(project_data, engine, optimizer), optimizer, project_data


def make_images(folder: Path):
    folder.mkdir()
    content = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><metadata>' + "x" * 500 + '</metadata><rect width="10" height="10"/></svg>'
    (folder / "logo.svg").write_text(content, encoding="utf-8")
    (folder / "Unicode 图像.svg").write_text(content, encoding="utf-8")
    (folder / "notes.txt").write_text("not an image", encoding="utf-8")


def test_local_scan_and_dry_run_never_modify_originals(workflow, tmp_path):
    service, optimizer, _ = workflow
    folder = tmp_path / "photos"
    make_images(folder)
    before = {path.name: path.read_bytes() for path in folder.glob("*.svg")}
    job_id = service.create(folder)
    assert service.scan(job_id) == 2
    report = service.dry_run(job_id)
    assert {path.name: path.read_bytes() for path in folder.glob("*.svg")} == before
    assert report.total_images == report.optimized == 2
    assert service.engine.repository.get(job_id).status is JobStatus.PREVIEW
    assert optimizer.calls == ["Unicode 图像.svg", "logo.svg"] or sorted(optimizer.calls) == sorted(before)


def test_apply_requires_confirmation_creates_backup_and_verifies(workflow, tmp_path):
    service, _, project_data = workflow
    folder = tmp_path / "photos"
    make_images(folder)
    original = (folder / "logo.svg").read_bytes()
    job_id = service.create(folder)
    service.scan(job_id)
    service.dry_run(job_id)
    with pytest.raises(PermissionError):
        service.apply(job_id, confirmed=False)
    report = service.apply(job_id, confirmed=True)
    backup = project_data / "backups" / "offline" / job_id / "logo.svg"
    assert backup.read_bytes() == original
    assert (folder / "logo.svg").read_bytes().startswith(b"<svg")
    assert report.optimized == 2 and report.failed == 0
    assert service.engine.repository.get(job_id).status is JobStatus.COMPLETED


def test_source_changed_after_preview_is_not_overwritten(workflow, tmp_path):
    service, _, _ = workflow
    folder = tmp_path / "photos"
    make_images(folder)
    job_id = service.create(folder)
    service.scan(job_id)
    service.dry_run(job_id)
    (folder / "logo.svg").write_text("user changed this", encoding="utf-8")
    report = service.apply(job_id, confirmed=True)
    assert (folder / "logo.svg").read_text(encoding="utf-8") == "user changed this"
    assert report.failed == 1


def test_crash_after_staging_reconciles_and_resumes_without_reoptimizing(workflow, tmp_path):
    service, optimizer, project_data = workflow
    folder = tmp_path / "photos"
    make_images(folder)
    job_id = service.create(folder)
    service.scan(job_id)
    service.dry_run(job_id)
    item = next(service.repository.iter_items(job_id, {LocalItemStatus.PROPOSED}))
    source, candidate = Path(item.source_path), Path(item.candidate_path)
    backup = project_data / "backups" / "offline" / job_id / item.relative_path
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    item.backup_path = str(backup)
    item.target_path = str(source)
    temporary = service._temporary_path(source, job_id)
    shutil.copyfile(candidate, temporary)
    item.status = LocalItemStatus.STAGED
    service.repository.save_item(item)

    # A fresh service instance represents a process restart.
    fresh = OfflineWorkflow(project_data, service.engine, optimizer, service.repository)
    fresh.recover(job_id)
    reconciled = next(fresh.repository.iter_items(job_id, {LocalItemStatus.REPLACED}))
    assert checksum(Path(reconciled.source_path)) == reconciled.candidate_checksum
    calls_before = list(optimizer.calls)
    report = fresh.apply(job_id, confirmed=True)
    assert optimizer.calls == calls_before
    assert report.failed == 0


def test_pause_resume_skips_already_processed_items(workflow, tmp_path):
    service, optimizer, _ = workflow
    folder = tmp_path / "photos"
    make_images(folder)
    job_id = service.create(folder)
    service.scan(job_id)
    service.pause(job_id)
    assert service.engine.repository.get(job_id).status is JobStatus.PAUSED
    report = service.resume(job_id)
    assert report.optimized == 2
    calls = list(optimizer.calls)
    service.dry_run(job_id)
    assert optimizer.calls == calls


def test_offline_report_and_database_schema(workflow, tmp_path):
    service, _, _ = workflow
    folder = tmp_path / "photos"
    make_images(folder)
    job_id = service.create(folder)
    service.scan(job_id)
    report = service.dry_run(job_id)
    assert report.original_bytes > report.final_bytes
    assert report.savings_bytes == report.original_bytes - report.final_bytes
    assert report.reduction_percent > 0 and report.formats_selected == {"SVG": 2}
    assert DATABASE_SCHEMA_VERSION == 8


def test_cancel_preserves_sources_and_proposals(workflow, tmp_path):
    service, _, _ = workflow
    folder = tmp_path / "photos"
    make_images(folder)
    before = (folder / "logo.svg").read_bytes()
    job_id = service.create(folder)
    service.scan(job_id)
    service.dry_run(job_id)
    service.cancel(job_id)
    assert service.engine.repository.get(job_id).status is JobStatus.CANCELLED
    assert (folder / "logo.svg").read_bytes() == before
    assert list(service.previews(job_id))


def test_database_migrates_version_four_to_offline_state(tmp_path):
    database = tmp_path / "v4.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=4")
    JobRepository(database).initialize()
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert {"offline_runs", "local_items"} <= tables
    assert version == 8
