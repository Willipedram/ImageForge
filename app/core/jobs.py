"""Durable job and item models with explicit state transitions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from app.core.version import APP_VERSION, DATA_SCHEMA_VERSION


class InvalidTransition(ValueError):
    """Raised before an invalid lifecycle transition can reach storage."""


class JobStatus(StrEnum):
    CREATED = "CREATED"
    PRECHECK = "PRECHECK"
    SCANNING = "SCANNING"
    DOWNLOADING = "DOWNLOADING"
    OPTIMIZING = "OPTIMIZING"
    VALIDATING = "VALIDATING"
    UPLOADING = "UPLOADING"
    DB_UPDATING = "DB_UPDATING"
    VERIFYING = "VERIFYING"
    CLEANUP = "CLEANUP"
    PAUSED = "PAUSED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RECOVERABLE = "RECOVERABLE"


class ItemStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    OPTIMIZING = "OPTIMIZING"
    OPTIMIZED = "OPTIMIZED"
    VERIFYING_LOCAL = "VERIFYING_LOCAL"
    READY_TO_UPLOAD = "READY_TO_UPLOAD"
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    VERIFYING_REMOTE = "VERIFYING_REMOTE"
    DB_UPDATE_PENDING = "DB_UPDATE_PENDING"
    DB_UPDATED = "DB_UPDATED"
    FINAL_VERIFIED = "FINAL_VERIFIED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


ACTIVE_JOB_STATES = frozenset({
    JobStatus.PRECHECK, JobStatus.SCANNING, JobStatus.DOWNLOADING,
    JobStatus.OPTIMIZING, JobStatus.VALIDATING, JobStatus.UPLOADING,
    JobStatus.DB_UPDATING, JobStatus.VERIFYING, JobStatus.CLEANUP,
    JobStatus.CANCELLING,
})
UNFINISHED_JOB_STATES = ACTIVE_JOB_STATES | {JobStatus.CREATED, JobStatus.PAUSED, JobStatus.RECOVERABLE}
TERMINAL_JOB_STATES = frozenset({JobStatus.CANCELLED, JobStatus.COMPLETED, JobStatus.FAILED})

_PIPELINE = [
    JobStatus.CREATED, JobStatus.PRECHECK, JobStatus.SCANNING,
    JobStatus.DOWNLOADING, JobStatus.OPTIMIZING, JobStatus.VALIDATING,
    JobStatus.UPLOADING, JobStatus.DB_UPDATING, JobStatus.VERIFYING,
    JobStatus.CLEANUP, JobStatus.COMPLETED,
]
JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    state: frozenset({next_state, JobStatus.PAUSED, JobStatus.CANCELLING, JobStatus.FAILED, JobStatus.RECOVERABLE})
    for state, next_state in zip(_PIPELINE, _PIPELINE[1:])
}
JOB_TRANSITIONS[JobStatus.CREATED] = JOB_TRANSITIONS[JobStatus.CREATED] - {JobStatus.PAUSED}
JOB_TRANSITIONS.update({
    JobStatus.PAUSED: frozenset({*ACTIVE_JOB_STATES - {JobStatus.CANCELLING}, JobStatus.CANCELLING, JobStatus.RECOVERABLE}),
    JobStatus.RECOVERABLE: frozenset({*ACTIVE_JOB_STATES - {JobStatus.CANCELLING}, JobStatus.CANCELLING, JobStatus.FAILED}),
    JobStatus.CANCELLING: frozenset({JobStatus.CANCELLED, JobStatus.RECOVERABLE, JobStatus.FAILED}),
    JobStatus.CANCELLED: frozenset(), JobStatus.COMPLETED: frozenset(), JobStatus.FAILED: frozenset(),
})

_ITEM_PIPELINE = list(ItemStatus)[:13]
ITEM_TRANSITIONS: dict[ItemStatus, frozenset[ItemStatus]] = {
    state: frozenset({next_state, ItemStatus.FAILED, ItemStatus.SKIPPED})
    for state, next_state in zip(_ITEM_PIPELINE, _ITEM_PIPELINE[1:])
}
ITEM_TRANSITIONS.update({
    ItemStatus.FINAL_VERIFIED: frozenset(), ItemStatus.FAILED: frozenset(), ItemStatus.SKIPPED: frozenset(),
})


def validate_transition(current: JobStatus | ItemStatus, new: JobStatus | ItemStatus) -> None:
    transitions = JOB_TRANSITIONS if isinstance(current, JobStatus) else ITEM_TRANSITIONS
    if type(current) is not type(new) or new not in transitions[current]:
        raise InvalidTransition(f"Cannot transition from {current.value} to {new.value}.")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class Job:
    target: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=utc_now)
    application_version: str = APP_VERSION
    data_schema_version: int = DATA_SCHEMA_VERSION
    status: JobStatus = JobStatus.CREATED
    progress: float = 0.0
    current_stage: str = "Waiting"
    resume_state: JobStatus | None = None
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None
    files_total: int = 0
    files_completed: int = 0
    original_bytes: int = 0
    optimized_bytes: int = 0
    saved_bytes: int = 0
    duration_seconds: float = 0.0

    def validate(self) -> None:
        if not 0.0 <= self.progress <= 100.0:
            raise ValueError("progress must be between 0 and 100")
        if min(self.files_total, self.files_completed, self.original_bytes, self.optimized_bytes,
               self.saved_bytes, self.duration_seconds) < 0:
            raise ValueError("job counters cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result = asdict(self)
        result["status"] = self.status.value
        result["resume_state"] = self.resume_state.value if self.resume_state else None
        return result

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Job":
        values = dict(payload)
        values["status"] = JobStatus(values["status"])
        if values.get("resume_state"):
            values["resume_state"] = JobStatus(values["resume_state"])
        job = cls(**values)
        job.validate()
        return job


@dataclass(slots=True)
class JobItem:
    job_id: str
    source: str
    id: str = field(default_factory=lambda: str(uuid4()))
    status: ItemStatus = ItemStatus.DISCOVERED
    local_path: str | None = None
    remote_path: str | None = None
    original_bytes: int = 0
    optimized_bytes: int = 0
    retry_count: int = 0
    last_error: str | None = None
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value
        return result

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "JobItem":
        values = dict(payload)
        values["status"] = ItemStatus(values["status"])
        return cls(**values)
