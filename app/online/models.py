"""Persistent online workflow records and deployment contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum


class OnlineItemStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    OPTIMIZING = "OPTIMIZING"
    READY = "READY"
    SKIPPED = "SKIPPED"
    BACKED_UP = "BACKED_UP"
    UPLOADING = "UPLOADING"
    STAGED = "STAGED"
    REMOTE_VERIFIED = "REMOTE_VERIFIED"
    PROMOTED = "PROMOTED"
    DB_PENDING = "DB_PENDING"
    DB_UPDATED = "DB_UPDATED"
    FINAL_VERIFIED = "FINAL_VERIFIED"
    FAILED = "FAILED"


@dataclass(slots=True)
class OnlineItem:
    id: str
    job_id: str
    remote_path: str
    original_bytes: int
    status: OnlineItemStatus = OnlineItemStatus.DISCOVERED
    original_checksum: str | None = None
    local_path: str | None = None
    local_checksum: str | None = None
    candidate_path: str | None = None
    candidate_format: str | None = None
    candidate_bytes: int | None = None
    candidate_checksum: str | None = None
    production_path: str | None = None
    staging_path: str | None = None
    decision: str | None = None
    decision_reason: str | None = None
    upload_status: str = "NOT_STARTED"
    verification_status: str = "NOT_STARTED"
    database_status: str = "NOT_STARTED"
    retry_count: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReferenceChange:
    original_path: str
    replacement_path: str


class ReferenceUpdater(ABC):
    """Transaction-oriented boundary for a future WordPress database adapter."""

    @abstractmethod
    def prepare(self, changes: tuple[ReferenceChange, ...]) -> None:
        """Validate access and create a rollback plan without changing references."""

    @abstractmethod
    def apply(self, changes: tuple[ReferenceChange, ...]) -> None:
        """Apply all prepared reference changes transactionally."""

    @abstractmethod
    def verify(self, changes: tuple[ReferenceChange, ...]) -> bool:
        """Return whether all references point at their intended replacements."""


@dataclass(frozen=True, slots=True)
class OnlineReport:
    job_id: str
    total: int
    completed: int
    skipped: int
    failed: int
    original_bytes: int
    candidate_bytes: int
    bytes_downloaded: int
    bytes_uploaded: int
    retries: int
    errors: tuple[str, ...]
