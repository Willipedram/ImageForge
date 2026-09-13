"""Sample-driven stage and overall ETA estimation."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class WorkStage(StrEnum):
    SCAN = "Scan"
    DOWNLOAD = "Download"
    OPTIMIZE = "Optimize"
    UPLOAD = "Upload"
    DATABASE = "Database"
    VERIFICATION = "Verification"
    CLEANUP = "Cleanup"


@dataclass(frozen=True, slots=True)
class WorkSample:
    stage: WorkStage
    completed_units: float
    elapsed_seconds: float
    bytes_processed: int = 0
    pixels_processed: int = 0
    file_type: str | None = None
    workers: int = 1


@dataclass(frozen=True, slots=True)
class StageEstimate:
    stage: WorkStage
    remaining_seconds: float | None
    confidence: float
    samples: int


class TimeEstimator:
    """Learns rates from real samples; unknown stages remain 'calculating'."""

    def __init__(self, history_rates: dict[WorkStage, float] | None = None, window: int = 30) -> None:
        self.history_rates = history_rates or {}
        self.samples: dict[WorkStage, deque[float]] = defaultdict(lambda: deque(maxlen=window))
        self.type_factors: dict[tuple[WorkStage, str], deque[float]] = defaultdict(lambda: deque(maxlen=window))

    def observe(self, sample: WorkSample) -> None:
        if sample.elapsed_seconds <= 0 or sample.completed_units <= 0: return
        complexity = self._complexity(sample.bytes_processed, sample.pixels_processed, sample.workers)
        rate = sample.completed_units * complexity / sample.elapsed_seconds
        self.samples[sample.stage].append(rate)
        if sample.file_type: self.type_factors[(sample.stage, sample.file_type.upper())].append(rate)

    def estimate(self, stage: WorkStage, remaining_units: float, *, remaining_bytes: int = 0,
                 remaining_pixels: int = 0, file_type: str | None = None, workers: int = 1) -> StageEstimate:
        rates = self.type_factors.get((stage, (file_type or "").upper())) or self.samples.get(stage)
        values = list(rates or ())
        if not values and stage in self.history_rates: values = [self.history_rates[stage]]
        if not values or remaining_units <= 0:
            return StageEstimate(stage, 0.0 if remaining_units <= 0 else None, 0.0 if not values else .25, len(values))
        # Recent weighted mean reacts to changing network/encoder conditions without jumping on one sample.
        weights = range(1, len(values) + 1); rate = sum(v * w for v, w in zip(values, weights)) / sum(weights)
        complexity = self._complexity(remaining_bytes, remaining_pixels, workers)
        seconds = remaining_units * complexity / max(rate, 1e-9)
        confidence = min(.95, .25 + len(values) / 12)
        return StageEstimate(stage, seconds, confidence, len(values))

    def overall(self, workloads: dict[WorkStage, dict[str, float | int | str]]) -> tuple[float | None, tuple[StageEstimate, ...]]:
        estimates = tuple(self.estimate(stage, float(work.get("units", 0)),
            remaining_bytes=int(work.get("bytes", 0)), remaining_pixels=int(work.get("pixels", 0)),
            file_type=str(work.get("file_type")) if work.get("file_type") else None,
            workers=int(work.get("workers", 1))) for stage, work in workloads.items())
        if any(estimate.remaining_seconds is None for estimate in estimates): return None, estimates
        return sum(estimate.remaining_seconds or 0 for estimate in estimates), estimates

    @staticmethod
    def eta_clock(seconds: float | None, now: datetime | None = None) -> str:
        if seconds is None: return "Calculating ETA..."
        return ((now or datetime.now(UTC)) + timedelta(seconds=max(0, seconds))).astimezone().strftime("%H:%M:%S")

    @staticmethod
    def _complexity(bytes_processed: int, pixels_processed: int, workers: int) -> float:
        byte_weight = max(1.0, bytes_processed / (4 * 1024 * 1024)) if bytes_processed else 1.0
        pixel_weight = max(1.0, pixels_processed / 4_000_000) if pixels_processed else 1.0
        return max(byte_weight, pixel_weight) / max(1, workers)


def format_duration(seconds: float | None) -> str:
    if seconds is None: return "Calculating ETA..."
    value = max(0, round(seconds)); hours, remainder = divmod(value, 3600); minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
