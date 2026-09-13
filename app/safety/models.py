"""Typed final-safety records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class BackupRetention(StrEnum):
    SEVEN_DAYS = "7_days"
    THIRTY_DAYS = "30_days"
    NINETY_DAYS = "90_days"
    NEVER = "never"

    @property
    def days(self) -> int | None:
        return {self.SEVEN_DAYS: 7, self.THIRTY_DAYS: 30,
                self.NINETY_DAYS: 90, self.NEVER: None}[self]


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    final_url: str


@dataclass(frozen=True, slots=True)
class HTTPVerification:
    passed: bool
    status: int | None
    mime_type: str | None
    detected_format: str | None
    width: int | None
    height: int | None
    cache_layers: tuple[str, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SafetyAudit:
    job_id: str
    item_id: str
    original_path: str
    candidate_path: str | None
    checks: dict[str, bool]
    deletion_authorized: bool
    original_deleted: bool = False
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FinalReport:
    job_id: str
    optimized: int
    skipped: int
    failed: int
    originals_deleted: int
    originals_retained: int
    database_records_updated: int
    verification_errors: tuple[str, ...]
    warnings: tuple[str, ...]
    total_savings: int
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class RollbackReport:
    job_id: str
    originals_restored: int
    candidates_removed: int
    database_restored: bool
    warnings: tuple[str, ...]
