"""Persistence for verified backup bundles, final audits, warnings, and rollback."""

from __future__ import annotations

import json

from app.core.jobs import utc_now
from app.database.jobs import JobRepository
from app.safety.models import SafetyAudit


class SafetyRepository:
    def __init__(self, jobs: JobRepository) -> None: self.jobs = jobs

    def save_bundle(self, job_id: str, path: str, checksum: str, verified: bool) -> None:
        with self.jobs.connection() as connection:
            connection.execute("""INSERT INTO backup_bundles VALUES(?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET bundle_path=excluded.bundle_path,
                manifest_checksum=excluded.manifest_checksum,verified=excluded.verified""",
                (job_id, path, checksum, int(verified), utc_now()))

    def bundle(self, job_id: str) -> dict | None:
        with self.jobs.connection() as connection:
            row = connection.execute("SELECT * FROM backup_bundles WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def save_audit(self, audit: SafetyAudit) -> None:
        now = utc_now()
        with self.jobs.connection() as connection:
            connection.execute("""INSERT INTO safety_audits(
                job_id,item_id,original_path,candidate_path,checks,deletion_authorized,
                original_deleted,warnings,errors,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,item_id) DO UPDATE SET candidate_path=excluded.candidate_path,
                checks=excluded.checks,deletion_authorized=excluded.deletion_authorized,
                original_deleted=excluded.original_deleted,warnings=excluded.warnings,
                errors=excluded.errors,updated_at=excluded.updated_at""",
                (audit.job_id, audit.item_id, audit.original_path, audit.candidate_path,
                 json.dumps(audit.checks, sort_keys=True), int(audit.deletion_authorized),
                 int(audit.original_deleted), json.dumps(audit.warnings), json.dumps(audit.errors), now, now))

    def audit(self, job_id: str, item_id: str) -> SafetyAudit | None:
        with self.jobs.connection() as connection:
            row = connection.execute("SELECT * FROM safety_audits WHERE job_id=? AND item_id=?",
                                     (job_id, item_id)).fetchone()
        if not row: return None
        return SafetyAudit(row["job_id"], row["item_id"], row["original_path"], row["candidate_path"],
            json.loads(row["checks"]), bool(row["deletion_authorized"]), bool(row["original_deleted"]),
            tuple(json.loads(row["warnings"])), tuple(json.loads(row["errors"])))

    def audits(self, job_id: str) -> tuple[SafetyAudit, ...]:
        with self.jobs.connection() as connection:
            ids = tuple(row[0] for row in connection.execute(
                "SELECT item_id FROM safety_audits WHERE job_id=? ORDER BY item_id", (job_id,)))
        return tuple(filter(None, (self.audit(job_id, item_id) for item_id in ids)))

    def rollback_event(self, job_id: str, status: str, detail: str) -> None:
        with self.jobs.connection() as connection:
            connection.execute("INSERT INTO rollback_events(job_id,status,detail,created_at) VALUES(?,?,?,?)",
                               (job_id, status, detail, utc_now()))

    def database_updates(self, job_id: str) -> int:
        with self.jobs.connection() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM database_reference_changes WHERE job_id=? AND status='UPDATED'", (job_id,)
            ).fetchone()[0])
