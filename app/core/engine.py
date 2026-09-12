"""Transactional job coordinator with checkpoint-based crash recovery."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.jobs import (
    ACTIVE_JOB_STATES, TERMINAL_JOB_STATES, InvalidTransition, ItemStatus, Job, JobItem,
    JobStatus, utc_now, validate_transition,
)
from app.core.retry import ErrorClassification, RetryPolicy
from app.database.jobs import JobRepository

Reconciler = Callable[[JobItem], ItemStatus | None]


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    job: Job
    last_safe_checkpoint: dict[str, Any] | None
    interrupted_items: tuple[JobItem, ...]


class JobEngine:
    """Owns all state transitions so state and checkpoints commit atomically."""

    INTERRUPTED_ITEM_STATES = frozenset({
        ItemStatus.DOWNLOADING, ItemStatus.OPTIMIZING, ItemStatus.VERIFYING_LOCAL,
        ItemStatus.UPLOADING, ItemStatus.VERIFYING_REMOTE,
    })

    def __init__(self, repository: JobRepository, retry_policy: RetryPolicy | None = None) -> None:
        self.repository = repository
        self.retry_policy = retry_policy or RetryPolicy()
        self.logger = logging.getLogger(__name__)

    def create_job(self, target: str) -> Job:
        normalized = target.strip().casefold()
        if not normalized:
            raise ValueError("A target is required.")
        job = Job(target=normalized)
        with self.repository.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.repository.save(job, connection)
            self.repository.acquire_lock(connection, normalized, job.id)
            connection.execute(
                "INSERT INTO job_stages(job_id,stage,entered_at) VALUES(?,?,?)",
                (job.id, job.status.value, utc_now()),
            )
            self.repository.event(connection, job.id, "JOB_CREATED", "Job created")
            self.repository.checkpoint(connection, job.id, "create", job.status.value)
        return job

    def transition(self, job_id: str, new_status: JobStatus, *, message: str | None = None) -> Job:
        job = self._required_job(job_id)
        validate_transition(job.status, new_status)
        previous = job.status
        if new_status is JobStatus.PAUSED:
            job.resume_state = previous
        elif new_status not in {JobStatus.RECOVERABLE, JobStatus.CANCELLING}:
            job.resume_state = None
        job.status = new_status
        job.current_stage = new_status.value.replace("_", " ").title()
        if job.started_at is None and new_status is JobStatus.PRECHECK:
            job.started_at = utc_now()
        if new_status in TERMINAL_JOB_STATES:
            job.ended_at = utc_now()
            if job.started_at:
                job.duration_seconds = max(
                    0.0, (datetime.fromisoformat(job.ended_at) - datetime.fromisoformat(job.started_at)).total_seconds()
                )
            job.saved_bytes = max(0, job.original_bytes - job.optimized_bytes)
            if new_status is JobStatus.COMPLETED:
                job.progress = 100.0
        with self.repository.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            persisted = connection.execute("SELECT status FROM jobs WHERE id=?", (job.id,)).fetchone()
            if persisted is None or persisted["status"] != previous.value:
                raise RuntimeError("Job state changed concurrently; reload it before retrying.")
            connection.execute(
                "UPDATE job_stages SET exited_at=?, outcome=? WHERE job_id=? AND exited_at IS NULL",
                (utc_now(), new_status.value, job.id),
            )
            connection.execute(
                "INSERT INTO job_stages(job_id,stage,entered_at) VALUES(?,?,?)",
                (job.id, new_status.value, utc_now()),
            )
            self.repository.save(job, connection)
            self.repository.event(connection, job.id, "STATE_CHANGED", message or f"{previous.value} → {new_status.value}")
            self.repository.checkpoint(
                connection, job.id, "state_transition", new_status.value,
                payload={"previous": previous.value, "resume_state": job.resume_state.value if job.resume_state else None},
            )
            if new_status in TERMINAL_JOB_STATES:
                self.repository.release_lock(connection, job.id)
        return job

    def add_item(self, item: JobItem) -> None:
        self._required_job(item.job_id)
        with self.repository.connection() as connection:
            self.repository.save_item(item, connection)
            self.repository.event(connection, item.job_id, "ITEM_DISCOVERED", item.source, item.id)
            self.repository.checkpoint(connection, item.job_id, "item_discovered", item.status.value, item.id)

    def update_statistics(self, job_id: str, *, files_total: int, files_completed: int,
                          original_bytes: int, optimized_bytes: int, progress: float) -> Job:
        """Atomically persist history counters and their recovery checkpoint."""
        job = self._required_job(job_id)
        job.files_total = files_total
        job.files_completed = files_completed
        job.original_bytes = original_bytes
        job.optimized_bytes = optimized_bytes
        job.saved_bytes = max(0, original_bytes - optimized_bytes)
        job.progress = progress
        with self.repository.connection() as connection:
            self.repository.save(job, connection)
            self.repository.event(connection, job.id, "STATISTICS_UPDATED", "Job statistics updated")
            self.repository.checkpoint(
                connection, job.id, "statistics", job.status.value,
                payload={"files_completed": files_completed, "progress": progress},
            )
        return job

    def transition_item(self, item: JobItem, new_status: ItemStatus) -> JobItem:
        validate_transition(item.status, new_status)
        previous = item.status
        item.status = new_status
        item.updated_at = utc_now()
        with self.repository.connection() as connection:
            self.repository.save_item(item, connection)
            self.repository.event(connection, item.job_id, "ITEM_STATE_CHANGED", f"{previous.value} → {new_status.value}", item.id)
            self.repository.checkpoint(connection, item.job_id, "item_transition", new_status.value, item.id)
        return item

    def pause(self, job_id: str) -> Job:
        return self.transition(job_id, JobStatus.PAUSED, message="Scheduling paused after the current safe operation")

    def resume(self, job_id: str, reconciler: Reconciler | None = None) -> Job:
        job = self._required_job(job_id)
        if job.status not in {JobStatus.CREATED, JobStatus.PAUSED, JobStatus.RECOVERABLE}:
            raise InvalidTransition(f"Job {job.id} is not resumable.")
        self.reconcile(job.id, reconciler)
        unresolved = [
            item for item in self.repository.list_items(job.id)
            if item.status in self.INTERRUPTED_ITEM_STATES
        ]
        if unresolved:
            raise RuntimeError(
                f"{len(unresolved)} interrupted item(s) require verification before this job can resume."
            )
        destination = job.resume_state or JobStatus.PRECHECK
        if destination not in ACTIVE_JOB_STATES or destination is JobStatus.CANCELLING:
            destination = JobStatus.PRECHECK
        return self.transition(job.id, destination, message="Resumed after checkpoint reconciliation")

    def cancel(self, job_id: str) -> Job:
        job = self._required_job(job_id)
        if job.status is not JobStatus.CANCELLING:
            job = self.transition(job_id, JobStatus.CANCELLING, message="Cancellation requested; stopping at a safe boundary")
        return self.transition(job.id, JobStatus.CANCELLED, message="Cancelled with durable state preserved")

    def recover_interrupted_jobs(self) -> list[RecoveryReport]:
        reports: list[RecoveryReport] = []
        for job in self.repository.list_jobs(unfinished_only=True):
            if job.status in ACTIVE_JOB_STATES:
                previous = job.status
                job.resume_state = previous if previous is not JobStatus.CANCELLING else JobStatus.PRECHECK
                job.status = JobStatus.RECOVERABLE
                job.current_stage = "Recovery required"
                with self.repository.connection() as connection:
                    self.repository.save(job, connection)
                    self.repository.event(connection, job.id, "CRASH_DETECTED", "Interrupted work requires reconciliation")
                    self.repository.checkpoint(connection, job.id, "crash_recovery", job.status.value,
                                               payload={"interrupted_state": previous.value}, is_safe=False)
            interrupted = tuple(
                item for item in self.repository.list_items(job.id)
                if item.status in self.INTERRUPTED_ITEM_STATES
            )
            reports.append(RecoveryReport(job, self.repository.last_safe_checkpoint(job.id), interrupted))
        return reports

    def reconcile(self, job_id: str, reconciler: Reconciler | None = None) -> None:
        """Verify interrupted side effects before retrying; never assumes they failed.

        Future remote adapters supply ``reconciler`` to inspect remote state. Without
        one, interrupted items remain unchanged and must not be scheduled.
        """
        self._required_job(job_id)
        interrupted = [item for item in self.repository.list_items(job_id) if item.status in self.INTERRUPTED_ITEM_STATES]
        with self.repository.connection() as connection:
            for item in interrupted:
                observed = reconciler(item) if reconciler else None
                if observed is not None and observed != item.status:
                    old = item.status
                    item.status = observed
                    item.updated_at = utc_now()
                    self.repository.save_item(item, connection)
                    self.repository.event(connection, job_id, "ITEM_RECONCILED", f"Observed {old.value} as {observed.value}", item.id)
                self.repository.checkpoint(
                    connection, job_id, "reconcile", item.status.value, item.id,
                    payload={"verified": observed is not None}, is_safe=observed is not None,
                )

    def record_error(self, job_id: str, classification: ErrorClassification, error: BaseException,
                     *, item_id: str | None = None, attempt: int = 1) -> bool:
        with self.repository.connection() as connection:
            connection.execute(
                "INSERT INTO errors(job_id,item_id,classification,error_type,message,attempt,created_at) VALUES(?,?,?,?,?,?,?)",
                (job_id, item_id, classification.value, type(error).__name__, str(error), attempt, utc_now()),
            )
            self.repository.event(connection, job_id, "ERROR", str(error), item_id,
                                  {"classification": classification.value, "attempt": attempt})
            self.repository.checkpoint(connection, job_id, "error", classification.value, item_id)
        return self.retry_policy.should_retry(classification, attempt)

    def _required_job(self, job_id: str) -> Job:
        job = self.repository.get(job_id)
        if job is None:
            raise KeyError(f"Unknown job: {job_id}")
        return job
