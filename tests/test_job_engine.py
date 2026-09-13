from __future__ import annotations

import sqlite3

import pytest

from app.core.engine import JobEngine
from app.core.jobs import InvalidTransition, ItemStatus, JobItem, JobStatus
from app.core.retry import ErrorClassification, RetryPolicy
from app.database.jobs import JobRepository, TargetLockedError


@pytest.fixture
def repository(tmp_path):
    result = JobRepository(tmp_path / "ProjectData" / "jobs" / "state.db")
    result.initialize()
    return result


def test_database_contains_all_durable_tables(repository):
    with sqlite3.connect(repository.database_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert {"jobs", "job_stages", "job_items", "events", "errors", "checkpoints"} <= tables
    assert version == 6


def test_state_machine_rejects_skipped_job_stage(repository):
    engine = JobEngine(repository)
    job = engine.create_job("https://example.com")
    with pytest.raises(InvalidTransition):
        engine.transition(job.id, JobStatus.OPTIMIZING)
    assert repository.get(job.id).status is JobStatus.CREATED


def test_items_transition_and_checkpoint(repository):
    engine = JobEngine(repository)
    job = engine.create_job("example.com")
    item = JobItem(job_id=job.id, source="/hero.png")
    engine.add_item(item)
    engine.transition_item(item, ItemStatus.DOWNLOADING)
    engine.transition_item(item, ItemStatus.DOWNLOADED)
    restored = repository.list_items(job.id)[0]
    assert restored.status is ItemStatus.DOWNLOADED
    assert repository.last_safe_checkpoint(job.id)["state"] == ItemStatus.DOWNLOADED.value


def test_simulated_crash_becomes_recoverable_and_resumes(repository):
    first_process = JobEngine(repository)
    job = first_process.create_job("example.com")
    first_process.transition(job.id, JobStatus.PRECHECK)
    first_process.transition(job.id, JobStatus.SCANNING)

    restarted_process = JobEngine(JobRepository(repository.database_path))
    restarted_process.repository.initialize()
    reports = restarted_process.recover_interrupted_jobs()
    assert len(reports) == 1
    assert reports[0].job.status is JobStatus.RECOVERABLE
    assert reports[0].last_safe_checkpoint["state"] == JobStatus.SCANNING.value
    resumed = restarted_process.resume(job.id)
    assert resumed.status is JobStatus.SCANNING


def test_interrupted_item_requires_reconciliation_before_external_state_advance(repository):
    engine = JobEngine(repository)
    job = engine.create_job("example.com")
    item = JobItem(job_id=job.id, source="/hero.png")
    engine.add_item(item)
    engine.transition_item(item, ItemStatus.DOWNLOADING)
    engine.transition_item(item, ItemStatus.DOWNLOADED)
    engine.transition_item(item, ItemStatus.OPTIMIZING)
    engine.transition_item(item, ItemStatus.OPTIMIZED)
    engine.transition_item(item, ItemStatus.VERIFYING_LOCAL)
    engine.transition_item(item, ItemStatus.READY_TO_UPLOAD)
    engine.transition_item(item, ItemStatus.UPLOADING)

    engine.reconcile(job.id)
    assert repository.list_items(job.id)[0].status is ItemStatus.UPLOADING
    recovered = repository.get(job.id)
    recovered.status = JobStatus.RECOVERABLE
    recovered.resume_state = JobStatus.UPLOADING
    repository.save(recovered)
    with pytest.raises(RuntimeError, match="require verification"):
        engine.resume(job.id)
    engine.reconcile(job.id, lambda _: ItemStatus.UPLOADED)
    assert repository.list_items(job.id)[0].status is ItemStatus.UPLOADED


def test_target_lock_conflict_and_release_on_cancel(repository):
    engine = JobEngine(repository)
    first = engine.create_job("Example.COM")
    with pytest.raises(TargetLockedError, match="currently active"):
        engine.create_job("example.com")
    engine.cancel(first.id)
    second = engine.create_job("example.com")
    assert second.id != first.id


def test_pause_resume_cancel_and_history(repository):
    engine = JobEngine(repository)
    job = engine.create_job("a.example")
    engine.transition(job.id, JobStatus.PRECHECK)
    assert engine.pause(job.id).status is JobStatus.PAUSED
    assert engine.resume(job.id).status is JobStatus.PRECHECK
    cancelled = engine.cancel(job.id)
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.ended_at is not None
    assert repository.list_jobs()[0].duration_seconds >= 0


def test_job_history_statistics_are_durable(repository):
    engine = JobEngine(repository)
    job = engine.create_job("history.example")
    saved = engine.update_statistics(
        job.id, files_total=10, files_completed=4,
        original_bytes=1_000, optimized_bytes=650, progress=40.0,
    )
    assert saved.saved_bytes == 350
    restored = repository.get(job.id)
    assert (restored.files_total, restored.files_completed, restored.saved_bytes) == (10, 4, 350)


def test_retry_policy_and_error_recording(repository):
    engine = JobEngine(repository, RetryPolicy(max_attempts=3, base_delay_seconds=2, maximum_delay_seconds=5))
    job = engine.create_job("a.example")
    assert engine.retry_policy.delay_for(3) == 5
    assert engine.record_error(job.id, ErrorClassification.RETRYABLE, TimeoutError("temporary"), attempt=1)
    assert not engine.record_error(job.id, ErrorClassification.NON_RETRYABLE, ValueError("bad"), attempt=1)
    with repository.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM errors WHERE job_id=?", (job.id,)).fetchone()[0] == 2
