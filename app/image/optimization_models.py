"""Configuration and durable decisions for offline image optimization."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class OptimizationDecision(StrEnum):
    SELECTED = "SELECTED"
    RETAINED_ORIGINAL = "RETAINED_ORIGINAL"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class OptimizationConfig:
    jpeg_quality: int = 85
    webp_quality: int = 82
    avif_quality: int = 55
    minimum_savings_ratio: float = 0.05
    minimum_savings_bytes: int = 1024
    minimum_psnr: float = 35.0
    max_pixels: int = 80_000_000
    max_file_bytes: int = 512 * 1024 * 1024
    timeout_seconds: float = 120.0
    max_workers: int = 2

    def __post_init__(self) -> None:
        if not all(1 <= quality <= 100 for quality in (self.jpeg_quality, self.webp_quality, self.avif_quality)):
            raise ValueError("Candidate quality must be between 1 and 100.")
        if not 0 <= self.minimum_savings_ratio < 1:
            raise ValueError("minimum_savings_ratio must be between 0 and 1.")
        if min(self.max_pixels, self.max_file_bytes, self.timeout_seconds, self.max_workers) <= 0:
            raise ValueError("Resource limits must be positive.")


@dataclass(slots=True)
class CandidateResult:
    path: str
    format: str
    bytes: int
    parameters: dict[str, object]
    width: int
    height: int
    has_alpha: bool
    transparency_ratio: float | None
    has_semitransparency: bool
    checksum: str
    validation_passed: bool
    validation_reason: str
    psnr: float | None = None


@dataclass(slots=True)
class OptimizationResult:
    job_id: str
    original_path: str
    original_bytes: int
    decision: OptimizationDecision
    decision_reason: str
    candidate_path: str | None = None
    candidate_format: str | None = None
    candidate_bytes: int | None = None
    quality_parameters: dict[str, object] = field(default_factory=dict)
    width: int | None = None
    height: int | None = None
    has_alpha: bool = False
    transparency_ratio: float | None = None
    has_semitransparency: bool = False
    checksum: str | None = None
    validation_passed: bool = False
    validation_reason: str = "Not validated"
    savings_bytes: int = 0
    savings_ratio: float = 0.0
