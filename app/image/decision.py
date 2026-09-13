"""Explainable quality, integrity, compatibility, and savings decisions."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

from app.image.models import AssetClass


class DecisionProfile(StrEnum):
    SAFE = "SAFE"
    BALANCED = "BALANCED"
    AGGRESSIVE = "AGGRESSIVE"


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class OutputChoice(StrEnum):
    ORIGINAL = "ORIGINAL"
    WEBP = "WEBP"
    AVIF = "AVIF"
    SKIP = "SKIP"


@dataclass(frozen=True, slots=True)
class ProfileThresholds:
    minimum_ssim: float
    minimum_psnr: float
    maximum_perceptual_difference: float
    minimum_savings_ratio: float
    minimum_savings_bytes: int


PROFILES = {
    DecisionProfile.SAFE: ProfileThresholds(.990, 40.0, .018, .08, 2048),
    DecisionProfile.BALANCED: ProfileThresholds(.975, 35.0, .035, .05, 1024),
    DecisionProfile.AGGRESSIVE: ProfileThresholds(.950, 31.0, .060, .03, 512),
}
STRICT_ASSETS = frozenset({
    AssetClass.LOGO, AssetClass.ICON, AssetClass.SCREENSHOT,
    AssetClass.TRANSPARENT_GRAPHIC, AssetClass.ILLUSTRATION,
})


@dataclass(frozen=True, slots=True)
class OriginalFacts:
    bytes: int
    width: int
    height: int
    has_alpha: bool
    transparency_ratio: float | None
    has_semitransparency: bool
    orientation: int | None = None
    color_valid: bool = True
    asset_class: AssetClass = AssetClass.UNKNOWN
    text_heavy: bool = False


@dataclass(slots=True)
class CandidateAssessment:
    format: str
    path: str
    bytes: int
    file_exists: bool = True
    compatible: bool = True
    signature_valid: bool = True
    decodable: bool = True
    width: int = 0
    height: int = 0
    orientation_correct: bool = True
    has_alpha: bool = False
    transparency_ratio: float | None = None
    has_semitransparency: bool = False
    color_valid: bool = True
    corruption_free: bool = True
    metrics: dict[str, float] = field(default_factory=dict)
    processing_seconds: float = 0.0
    parameters: dict[str, object] = field(default_factory=dict)
    checksum: str = ""


@dataclass(frozen=True, slots=True)
class CandidateVerdict:
    candidate: CandidateAssessment
    accepted: bool
    confidence: Confidence
    reasons: tuple[str, ...]
    savings_ratio: float
    score: float


@dataclass(frozen=True, slots=True)
class FormatDecision:
    choice: OutputChoice
    confidence: Confidence
    reason: str
    selected: CandidateAssessment | None
    verdicts: tuple[CandidateVerdict, ...]
    profile: DecisionProfile


class FormatDecisionEngine:
    def __init__(self, profile: DecisionProfile = DecisionProfile.SAFE,
                 thresholds: ProfileThresholds | None = None) -> None:
        self.profile = profile
        self.thresholds = thresholds or PROFILES[profile]

    def decide(self, original: OriginalFacts, candidates: list[CandidateAssessment]) -> FormatDecision:
        if not original.color_valid:
            return FormatDecision(OutputChoice.SKIP, Confidence.HIGH, "Skipped: original color information is uncertain.", None, (), self.profile)
        verdicts = tuple(self._assess(original, candidate) for candidate in candidates if candidate.format in {"WEBP", "AVIF"})
        eligible = [verdict for verdict in verdicts if verdict.accepted and verdict.confidence is not Confidence.LOW]
        if not eligible:
            reason = self._retention_reason(verdicts)
            return FormatDecision(OutputChoice.ORIGINAL, Confidence.HIGH, reason, None, verdicts, self.profile)
        selected = max(eligible, key=lambda verdict: (verdict.score, -verdict.candidate.bytes))
        rejected_smaller = [
            verdict for verdict in verdicts
            if not verdict.accepted and verdict.candidate.bytes < selected.candidate.bytes
        ]
        savings = selected.savings_ratio * 100
        reason = f"{selected.candidate.format} selected: {savings:.1f}% smaller with acceptable perceptual difference."
        if rejected_smaller:
            smallest = min(rejected_smaller, key=lambda verdict: verdict.candidate.bytes)
            reason += f" {smallest.candidate.format} was smaller but {smallest.reasons[0].lower()}"
        return FormatDecision(OutputChoice(selected.candidate.format), selected.confidence, reason, selected.candidate, verdicts, self.profile)

    def _assess(self, original: OriginalFacts, candidate: CandidateAssessment) -> CandidateVerdict:
        reasons: list[str] = []
        integrity = (
            (candidate.file_exists, "candidate file is missing"),
            (candidate.compatible, "candidate format is not compatible with the target policy"),
            (candidate.signature_valid, "file signature is invalid"),
            (candidate.decodable, "candidate cannot be decoded"),
            (candidate.corruption_free, "candidate shows corruption"),
            ((candidate.width, candidate.height) == (original.width, original.height), "dimensions changed"),
            (candidate.orientation_correct, "orientation changed"),
            (candidate.color_valid, "color information is invalid or uncertain"),
            (candidate.has_alpha == original.has_alpha, "transparency mismatch"),
            (candidate.has_semitransparency == original.has_semitransparency, "semitransparency mismatch"),
        )
        reasons.extend(reason for passed, reason in integrity if not passed)
        if original.has_alpha and not _ratio_matches(original.transparency_ratio, candidate.transparency_ratio):
            reasons.append("transparent regions changed")

        threshold = self.thresholds
        strict = original.asset_class in STRICT_ASSETS or original.text_heavy
        minimum_ssim = min(.999, threshold.minimum_ssim + (.007 if strict else 0))
        minimum_psnr = threshold.minimum_psnr + (3.0 if strict else 0)
        maximum_difference = threshold.maximum_perceptual_difference * (.6 if strict else 1)
        if "ssim" in candidate.metrics and candidate.metrics["ssim"] < minimum_ssim:
            reasons.append(f"SSIM {candidate.metrics['ssim']:.4f} is below {minimum_ssim:.4f}")
        if "psnr" in candidate.metrics and candidate.metrics["psnr"] < minimum_psnr:
            reasons.append(f"PSNR {candidate.metrics['psnr']:.2f} dB is below {minimum_psnr:.2f} dB")
        if ("perceptual_difference" in candidate.metrics and
                candidate.metrics["perceptual_difference"] > maximum_difference):
            reasons.append("perceptual difference exceeds the quality threshold")
        if not candidate.metrics:
            reasons.append("no perceptual quality metric is available")

        savings_bytes = original.bytes - candidate.bytes
        savings_ratio = savings_bytes / max(1, original.bytes)
        if savings_bytes < threshold.minimum_savings_bytes or savings_ratio < threshold.minimum_savings_ratio:
            reasons.append("candidate savings are below the minimum threshold")
        confidence = self._confidence(candidate, strict, minimum_ssim, minimum_psnr)
        if confidence is Confidence.LOW:
            reasons.append("quality confidence is low")
        quality = _quality_score(candidate.metrics)
        cost_penalty = min(.12, candidate.processing_seconds / 300)
        score = savings_ratio * .55 + quality * .45 - cost_penalty
        return CandidateVerdict(candidate, not reasons, confidence, tuple(reasons), savings_ratio, score)

    @staticmethod
    def _confidence(candidate: CandidateAssessment, strict: bool, minimum_ssim: float,
                    minimum_psnr: float) -> Confidence:
        metric_count = len(candidate.metrics)
        if metric_count == 0:
            return Confidence.LOW
        ssim_margin = candidate.metrics.get("ssim", minimum_ssim) - minimum_ssim
        psnr_margin = candidate.metrics.get("psnr", minimum_psnr) - minimum_psnr
        if metric_count >= 2 and ssim_margin >= (.004 if strict else .008) and psnr_margin >= 2:
            return Confidence.HIGH
        return Confidence.MEDIUM

    @staticmethod
    def _retention_reason(verdicts: tuple[CandidateVerdict, ...]) -> str:
        if not verdicts:
            return "Original retained: no compatible WebP or AVIF candidate was available."
        all_reasons = [reason for verdict in verdicts for reason in verdict.reasons]
        priorities = (
            "transparency mismatch", "transparent regions changed", "dimensions changed",
            "candidate cannot be decoded", "file signature is invalid",
            "candidate savings are below the minimum threshold", "quality confidence is low",
        )
        for priority in priorities:
            if priority in all_reasons:
                return f"Original retained: {priority}."
        return f"Original retained: {all_reasons[0] if all_reasons else 'candidate validation was uncertain'}."


def _ratio_matches(expected: float | None, actual: float | None) -> bool:
    if expected is None or actual is None:
        return expected is actual
    return math.isclose(expected, actual, rel_tol=0, abs_tol=1e-6)


def _quality_score(metrics: dict[str, float]) -> float:
    scores = []
    if "ssim" in metrics: scores.append(max(0.0, min(1.0, metrics["ssim"])))
    if "psnr" in metrics: scores.append(max(0.0, min(1.0, metrics["psnr"] / 60)))
    if "perceptual_difference" in metrics: scores.append(max(0.0, 1 - metrics["perceptual_difference"]))
    return sum(scores) / len(scores) if scores else 0.0
