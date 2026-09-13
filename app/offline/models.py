"""Persistent local workflow records and reports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LocalItemStatus(StrEnum):
    SCANNED = "SCANNED"
    OPTIMIZING = "OPTIMIZING"
    PROPOSED = "PROPOSED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    APPLYING = "APPLYING"
    BACKED_UP = "BACKED_UP"
    STAGED = "STAGED"
    REPLACED = "REPLACED"
    APPLIED = "APPLIED"


@dataclass(slots=True)
class LocalItem:
    id: str
    job_id: str
    relative_path: str
    source_path: str
    original_bytes: int
    original_checksum: str
    modified_ns: int
    status: LocalItemStatus = LocalItemStatus.SCANNED
    candidate_path: str | None = None
    candidate_format: str | None = None
    candidate_bytes: int | None = None
    candidate_checksum: str | None = None
    decision_reason: str | None = None
    width: int | None = None
    height: int | None = None
    backup_path: str | None = None
    target_path: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class OfflineReport:
    job_id: str
    total_images: int
    optimized: int
    skipped: int
    failed: int
    original_bytes: int
    final_bytes: int
    savings_bytes: int
    reduction_percent: float
    formats_selected: dict[str, int]
    duration_seconds: float
    errors: tuple[str, ...]
