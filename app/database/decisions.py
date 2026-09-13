"""Reproducible, append-auditable format decision manifests."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from app.core.jobs import utc_now
from app.database.jobs import JobRepository
from app.image.decision import FormatDecision, OriginalFacts, ProfileThresholds
from app.image.optimization_models import OptimizationResult

DECISION_ENGINE_VERSION = "1"


class DecisionManifestRepository:
    def __init__(self, jobs: JobRepository) -> None:
        self.jobs = jobs

    def save(self, job_id: str, original_path: str, original: OriginalFacts,
             decision: FormatDecision, thresholds: ProfileThresholds) -> None:
        candidates = [asdict(verdict.candidate) for verdict in decision.verdicts]
        verdicts = [
            {
                "format": verdict.candidate.format, "accepted": verdict.accepted,
                "confidence": verdict.confidence.value, "reasons": verdict.reasons,
                "savings_ratio": verdict.savings_ratio, "score": verdict.score,
            }
            for verdict in decision.verdicts
        ]
        with self.jobs.connection() as connection:
            connection.execute(
                """INSERT INTO decision_manifests(
                job_id,original_path,engine_version,profile,thresholds,original_facts,
                candidate_assessments,verdicts,selected_format,confidence,reason,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,original_path) DO UPDATE SET
                engine_version=excluded.engine_version,profile=excluded.profile,
                thresholds=excluded.thresholds,original_facts=excluded.original_facts,
                candidate_assessments=excluded.candidate_assessments,verdicts=excluded.verdicts,
                selected_format=excluded.selected_format,confidence=excluded.confidence,
                reason=excluded.reason,created_at=excluded.created_at""",
                (
                    job_id, original_path, DECISION_ENGINE_VERSION, decision.profile.value,
                    _json(asdict(thresholds)), _json(asdict(original)), _json(candidates), _json(verdicts),
                    decision.choice.value, decision.confidence.value, decision.reason, utc_now(),
                ),
            )

    def get(self, job_id: str, original_path: str) -> dict[str, Any] | None:
        with self.jobs.connection() as connection:
            row = connection.execute(
                "SELECT * FROM decision_manifests WHERE job_id=? AND original_path=?",
                (job_id, original_path),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        for key in ("thresholds", "original_facts", "candidate_assessments", "verdicts"):
            result[key] = json.loads(result[key])
        return result

    def save_terminal_result(self, result: OptimizationResult, profile: str,
                             thresholds: ProfileThresholds) -> None:
        """Audit decisions made before candidate evaluation (skip/retain/SVG)."""
        with self.jobs.connection() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO decision_manifests(
                job_id,original_path,engine_version,profile,thresholds,original_facts,
                candidate_assessments,verdicts,selected_format,confidence,reason,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result.job_id, result.original_path, DECISION_ENGINE_VERSION, profile,
                    _json(asdict(thresholds)), _json({
                        "bytes": result.original_bytes, "width": result.width, "height": result.height,
                        "has_alpha": result.has_alpha, "transparency_ratio": result.transparency_ratio,
                        "has_semitransparency": result.has_semitransparency,
                    }), "[]", "[]",
                    result.candidate_format or ("SKIP" if result.decision.value in {"SKIPPED", "FAILED"} else "ORIGINAL"),
                    result.confidence, result.decision_reason, utc_now(),
                ),
            )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
