"""SQLite persistence for offline candidate decisions."""

from __future__ import annotations

import json

from app.core.jobs import utc_now
from app.database.jobs import JobRepository
from app.image.optimization_models import OptimizationDecision, OptimizationResult


class OptimizationRepository:
    def __init__(self, jobs: JobRepository) -> None:
        self.jobs = jobs

    def save(self, result: OptimizationResult) -> None:
        with self.jobs.connection() as connection:
            connection.execute(
                """INSERT INTO optimization_results(
                job_id,original_path,original_bytes,candidate_path,candidate_format,candidate_bytes,
                quality_parameters,width,height,has_alpha,transparency_ratio,has_semitransparency,
                checksum,validation_passed,validation_reason,confidence,savings_bytes,savings_ratio,decision,decision_reason,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,original_path) DO UPDATE SET
                candidate_path=excluded.candidate_path,candidate_format=excluded.candidate_format,
                candidate_bytes=excluded.candidate_bytes,quality_parameters=excluded.quality_parameters,
                width=excluded.width,height=excluded.height,has_alpha=excluded.has_alpha,
                transparency_ratio=excluded.transparency_ratio,has_semitransparency=excluded.has_semitransparency,
                checksum=excluded.checksum,validation_passed=excluded.validation_passed,
                validation_reason=excluded.validation_reason,
                confidence=excluded.confidence,
                savings_bytes=excluded.savings_bytes,savings_ratio=excluded.savings_ratio,
                decision=excluded.decision,decision_reason=excluded.decision_reason,created_at=excluded.created_at""",
                (
                    result.job_id, result.original_path, result.original_bytes, result.candidate_path,
                    result.candidate_format, result.candidate_bytes, json.dumps(result.quality_parameters, sort_keys=True),
                    result.width, result.height, result.has_alpha, result.transparency_ratio,
                    result.has_semitransparency, result.checksum, result.validation_passed,
                    result.validation_reason,
                    result.confidence,
                    result.savings_bytes, result.savings_ratio, result.decision.value,
                    result.decision_reason, utc_now(),
                ),
            )

    def get(self, job_id: str, original_path: str) -> OptimizationResult | None:
        with self.jobs.connection() as connection:
            row = connection.execute(
                "SELECT * FROM optimization_results WHERE job_id=? AND original_path=?", (job_id, original_path)
            ).fetchone()
        if not row:
            return None
        return OptimizationResult(
            job_id=row["job_id"], original_path=row["original_path"], original_bytes=row["original_bytes"],
            candidate_path=row["candidate_path"], candidate_format=row["candidate_format"],
            candidate_bytes=row["candidate_bytes"], quality_parameters=json.loads(row["quality_parameters"]),
            width=row["width"], height=row["height"], has_alpha=bool(row["has_alpha"]),
            transparency_ratio=row["transparency_ratio"], has_semitransparency=bool(row["has_semitransparency"]),
            checksum=row["checksum"], validation_passed=bool(row["validation_passed"]),
            validation_reason=row["validation_reason"],
            confidence=row["confidence"],
            savings_bytes=row["savings_bytes"], savings_ratio=row["savings_ratio"],
            decision=OptimizationDecision(row["decision"]), decision_reason=row["decision_reason"],
        )
