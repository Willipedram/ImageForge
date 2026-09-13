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
    minimum_savings_ratio: float | None = None
    minimum_savings_bytes: int | None = None
    minimum_psnr: float | None = None
    max_pixels: int = 80_000_000
    max_file_bytes: int = 512 * 1024 * 1024
    timeout_seconds: float = 120.0
    max_workers: int = 2
    decision_profile: str = "SAFE"
    compatible_formats: tuple[str, ...] = ("WEBP", "AVIF")

    def __post_init__(self) -> None:
        if not all(1 <= quality <= 100 for quality in (self.jpeg_quality, self.webp_quality, self.avif_quality)):
            raise ValueError("Candidate quality must be between 1 and 100.")
        if self.minimum_savings_ratio is not None and not 0 <= self.minimum_savings_ratio < 1:
            raise ValueError("minimum_savings_ratio must be between 0 and 1.")
        if self.minimum_savings_bytes is not None and self.minimum_savings_bytes < 0:
            raise ValueError("minimum_savings_bytes cannot be negative.")
        if self.minimum_psnr is not None and self.minimum_psnr <= 0:
            raise ValueError("minimum_psnr must be positive.")
        if min(self.max_pixels, self.max_file_bytes, self.timeout_seconds, self.max_workers) <= 0:
            raise ValueError("Resource limits must be positive.")
        if self.decision_profile not in {"SAFE", "BALANCED", "AGGRESSIVE"}:
            raise ValueError("decision_profile must be SAFE, BALANCED, or AGGRESSIVE.")
        if not set(self.compatible_formats) <= {"WEBP", "AVIF"}:
            raise ValueError("compatible_formats contains an unsupported output format.")


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
    metrics: dict[str, float] = field(default_factory=dict)
    orientation_correct: bool = True
    color_valid: bool = True
    corruption_free: bool = True
    processing_seconds: float = 0.0
    file_exists: bool = True
    signature_valid: bool = True
    decodable: bool = True


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
    confidence: str = "LOW"
    savings_bytes: int = 0
    savings_ratio: float = 0.0
