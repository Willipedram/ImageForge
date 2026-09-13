from __future__ import annotations

from app.core.engine import JobEngine
from app.database.decisions import DecisionManifestRepository
from app.database.jobs import DATABASE_SCHEMA_VERSION, JobRepository
from app.image.decision import (
    CandidateAssessment, Confidence, DecisionProfile, FormatDecisionEngine,
    OriginalFacts, OutputChoice, PROFILES,
)
from app.image.models import AssetClass
from app.image.quality import QualityMetric, QualityPipeline


def original(**changes):
    values = dict(bytes=100_000, width=1000, height=800, has_alpha=False,
                  transparency_ratio=None, has_semitransparency=False,
                  asset_class=AssetClass.PHOTO)
    values.update(changes)
    return OriginalFacts(**values)


def candidate(format_name, size, *, ssim=.99, psnr=40.0, difference=.01, **changes):
    values = dict(
        format=format_name, path=f"/tmp/candidate.{format_name.casefold()}", bytes=size,
        width=1000, height=800,
        metrics={"ssim": ssim, "psnr": psnr, "perceptual_difference": difference},
    )
    values.update(changes)
    return CandidateAssessment(**values)


def test_avif_smaller_but_lower_quality_selects_webp_with_reason():
    engine = FormatDecisionEngine(DecisionProfile.BALANCED)
    decision = engine.decide(original(), [
        candidate("AVIF", 35_000, ssim=.94, psnr=29, difference=.07),
        candidate("WEBP", 50_000, ssim=.995, psnr=44, difference=.006),
    ])
    assert decision.choice is OutputChoice.WEBP
    assert "AVIF was smaller" in decision.reason
    assert "quality" in decision.reason.casefold() or "ssim" in decision.reason.casefold()


def test_webp_slightly_larger_can_win_on_quality_and_cost():
    decision = FormatDecisionEngine(DecisionProfile.BALANCED).decide(original(), [
        candidate("AVIF", 50_000, ssim=.98, psnr=36, difference=.03, processing_seconds=20),
        candidate("WEBP", 55_000, ssim=.998, psnr=48, difference=.002, processing_seconds=1),
    ])
    assert decision.choice is OutputChoice.WEBP


def test_original_retained_when_it_is_smaller_or_savings_are_trivial():
    decision = FormatDecisionEngine().decide(original(bytes=10_000), [
        candidate("WEBP", 10_100), candidate("AVIF", 9_900),
    ])
    assert decision.choice is OutputChoice.ORIGINAL
    assert "savings" in decision.reason.casefold()


def test_transparency_mismatch_immediately_rejects_candidate():
    facts = original(has_alpha=True, transparency_ratio=.4, has_semitransparency=True)
    decision = FormatDecisionEngine().decide(facts, [
        candidate("WEBP", 40_000, has_alpha=False, transparency_ratio=None, has_semitransparency=False),
    ])
    assert decision.choice is OutputChoice.ORIGINAL
    assert decision.reason == "Original retained: transparency mismatch."


def test_dimension_mismatch_and_corrupt_candidate_are_rejected():
    decision = FormatDecisionEngine().decide(original(), [
        candidate("WEBP", 20_000, width=999),
        candidate("AVIF", 15_000, decodable=False, corruption_free=False),
    ])
    assert decision.choice is OutputChoice.ORIGINAL
    assert "dimensions" in decision.reason or "decode" in decision.reason
    assert not any(verdict.accepted for verdict in decision.verdicts)


def test_missing_signature_orientation_color_and_compatibility_fail_integrity_gate():
    decision = FormatDecisionEngine().decide(original(), [
        candidate("WEBP", 20_000, file_exists=False),
        candidate("AVIF", 20_000, signature_valid=False, orientation_correct=False,
                  color_valid=False, compatible=False),
    ])
    reasons = {reason for verdict in decision.verdicts for reason in verdict.reasons}
    assert {"candidate file is missing", "file signature is invalid", "orientation changed",
            "color information is invalid or uncertain",
            "candidate format is not compatible with the target policy"} <= reasons


def test_quality_pipeline_accepts_replaceable_evaluators():
    class Evaluator:
        name = "future_metric"
        def evaluate(self, reference, candidate):
            return QualityMetric(self.name, .75)

    metrics = QualityPipeline((Evaluator(),)).evaluate(object(), object())
    assert metrics == {"future_metric": QualityMetric("future_metric", .75)}


def test_logo_threshold_is_stricter_than_photo_threshold():
    engine = FormatDecisionEngine(DecisionProfile.BALANCED)
    assessment = candidate("WEBP", 50_000, ssim=.98, psnr=36, difference=.025)
    photo = engine.decide(original(asset_class=AssetClass.PHOTO), [assessment])
    logo = engine.decide(original(asset_class=AssetClass.LOGO), [assessment])
    assert photo.choice is OutputChoice.WEBP
    assert logo.choice is OutputChoice.ORIGINAL


def test_profiles_and_low_confidence_are_conservative():
    borderline = candidate("AVIF", 70_000, ssim=.96, psnr=32, difference=.05)
    assert FormatDecisionEngine(DecisionProfile.SAFE).decide(original(), [borderline]).choice is OutputChoice.ORIGINAL
    assert FormatDecisionEngine(DecisionProfile.AGGRESSIVE).decide(original(), [borderline]).choice is OutputChoice.AVIF
    no_metrics = candidate("WEBP", 20_000)
    no_metrics.metrics = {}
    decision = FormatDecisionEngine(DecisionProfile.AGGRESSIVE).decide(original(), [no_metrics])
    assert decision.choice is OutputChoice.ORIGINAL
    assert decision.verdicts[0].confidence is Confidence.LOW


def test_auditable_manifest_persists_inputs_thresholds_and_all_verdicts(tmp_path):
    jobs = JobRepository(tmp_path / "state.db")
    jobs.initialize()
    job = JobEngine(jobs).create_job("example.com")
    facts = original()
    assessments = [candidate("WEBP", 50_000), candidate("AVIF", 45_000, ssim=.96)]
    engine = FormatDecisionEngine(DecisionProfile.SAFE)
    decision = engine.decide(facts, assessments)
    repository = DecisionManifestRepository(jobs)
    repository.save(job.id, "/uploads/photo.jpg", facts, decision, PROFILES[DecisionProfile.SAFE])
    manifest = repository.get(job.id, "/uploads/photo.jpg")
    assert manifest["selected_format"] == decision.choice.value
    assert manifest["profile"] == "SAFE" and len(manifest["verdicts"]) == 2
    assert manifest["candidate_assessments"][0]["metrics"]["ssim"] == .99
    assert manifest["engine_version"] == "1"
    assert DATABASE_SCHEMA_VERSION == 8
