"""Paged persistence for resumable local-folder workflows."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator

from app.core.jobs import utc_now
from app.database.jobs import JobRepository
from app.offline.models import LocalItem, LocalItemStatus, OfflineReport


class OfflineRepository:
    def __init__(self, jobs: JobRepository) -> None:
        self.jobs = jobs

    def create_run(self, job_id: str, root: str, dry_run: bool = True) -> None:
        now = utc_now()
        with self.jobs.connection() as connection:
            connection.execute(
                "INSERT INTO offline_runs(job_id,local_root,dry_run,created_at,updated_at) VALUES(?,?,?,?,?)",
                (job_id, root, int(dry_run), now, now),
            )

    def run(self, job_id: str) -> dict | None:
        with self.jobs.connection() as connection:
            row = connection.execute("SELECT * FROM offline_runs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def latest_unfinished_job(self) -> str | None:
        with self.jobs.connection() as connection:
            row = connection.execute(
                """SELECT offline_runs.job_id FROM offline_runs JOIN jobs ON jobs.id=offline_runs.job_id
                WHERE jobs.status NOT IN ('COMPLETED','CANCELLED','FAILED')
                ORDER BY offline_runs.created_at DESC LIMIT 1"""
            ).fetchone()
        return str(row[0]) if row else None

    def set_dry_run(self, job_id: str, dry_run: bool) -> None:
        with self.jobs.connection() as connection:
            connection.execute(
                "UPDATE offline_runs SET dry_run=?,updated_at=? WHERE job_id=?",
                (int(dry_run), utc_now(), job_id),
            )

    def save_items(self, items: Iterable[LocalItem]) -> int:
        rows = [self._values(item) for item in items]
        if not rows:
            return 0
        with self.jobs.connection() as connection:
            connection.executemany(
                """INSERT INTO local_items(
                id,job_id,relative_path,source_path,original_bytes,original_checksum,modified_ns,status,
                candidate_path,candidate_format,candidate_bytes,candidate_checksum,decision_reason,width,height,
                backup_path,target_path,error,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,relative_path) DO NOTHING""", rows,
            )
        return len(rows)

    def save_item(self, item: LocalItem) -> None:
        with self.jobs.connection() as connection:
            connection.execute(
                """UPDATE local_items SET status=?,candidate_path=?,candidate_format=?,candidate_bytes=?,
                candidate_checksum=?,decision_reason=?,width=?,height=?,backup_path=?,target_path=?,error=?,updated_at=?
                WHERE id=? AND job_id=?""",
                (item.status.value, item.candidate_path, item.candidate_format, item.candidate_bytes,
                 item.candidate_checksum, item.decision_reason, item.width, item.height,
                 item.backup_path, item.target_path, item.error, utc_now(), item.id, item.job_id),
            )

    def iter_items(self, job_id: str, statuses: set[LocalItemStatus] | None = None,
                   page_size: int = 250) -> Iterator[LocalItem]:
        last_id = ""
        status_values = tuple(status.value for status in statuses or ())
        while True:
            where, params = "job_id=? AND id>?", [job_id, last_id]
            if status_values:
                where += f" AND status IN ({','.join('?' for _ in status_values)})"
                params.extend(status_values)
            params.append(page_size)
            with self.jobs.connection() as connection:
                rows = connection.execute(
                    f"SELECT * FROM local_items WHERE {where} ORDER BY id LIMIT ?", tuple(params)
                ).fetchall()
            if not rows:
                return
            for row in rows:
                last_id = row["id"]
                yield self._item(row)

    def count(self, job_id: str) -> int:
        with self.jobs.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM local_items WHERE job_id=?", (job_id,)).fetchone()[0])

    def count_statuses(self, job_id: str, statuses: set[LocalItemStatus]) -> int:
        values = tuple(status.value for status in statuses)
        with self.jobs.connection() as connection:
            return int(connection.execute(
                f"SELECT COUNT(*) FROM local_items WHERE job_id=? AND status IN ({','.join('?' for _ in values)})",
                (job_id, *values),
            ).fetchone()[0])

    def byte_totals(self, job_id: str) -> tuple[int, int]:
        with self.jobs.connection() as connection:
            row = connection.execute(
                """SELECT COALESCE(SUM(original_bytes),0),
                COALESCE(SUM(CASE WHEN candidate_bytes IS NOT NULL THEN candidate_bytes ELSE original_bytes END),0)
                FROM local_items WHERE job_id=?""", (job_id,),
            ).fetchone()
        return int(row[0]), int(row[1])

    def report(self, job_id: str, duration_seconds: float) -> OfflineReport:
        with self.jobs.connection() as connection:
            row = connection.execute(
                """SELECT COUNT(*),SUM(status IN ('PROPOSED','APPLIED')),SUM(status='SKIPPED'),SUM(status='FAILED'),
                COALESCE(SUM(original_bytes),0),
                COALESCE(SUM(CASE WHEN status IN ('PROPOSED','APPLIED') THEN candidate_bytes ELSE original_bytes END),0)
                FROM local_items WHERE job_id=?""", (job_id,),
            ).fetchone()
            formats = dict(connection.execute(
                "SELECT candidate_format,COUNT(*) FROM local_items WHERE job_id=? AND status IN ('PROPOSED','APPLIED') GROUP BY candidate_format",
                (job_id,),
            ).fetchall())
            errors = tuple(value[0] for value in connection.execute(
                "SELECT error FROM local_items WHERE job_id=? AND error IS NOT NULL ORDER BY relative_path", (job_id,),
            ).fetchall())
        total, optimized, skipped, failed, original, final = (int(value or 0) for value in row)
        savings = max(0, original - final)
        return OfflineReport(
            job_id, total, optimized, skipped, failed, original, final, savings,
            savings / original * 100 if original else 0.0, formats, duration_seconds, errors,
        )

    @staticmethod
    def _values(item: LocalItem) -> tuple:
        return (
            item.id, item.job_id, item.relative_path, item.source_path, item.original_bytes,
            item.original_checksum, item.modified_ns, item.status.value, item.candidate_path,
            item.candidate_format, item.candidate_bytes, item.candidate_checksum,
            item.decision_reason, item.width, item.height, item.backup_path,
            item.target_path, item.error, utc_now(),
        )

    @staticmethod
    def _item(row: sqlite3.Row) -> LocalItem:
        values = dict(row)
        values.pop("updated_at")
        values["status"] = LocalItemStatus(values["status"])
        return LocalItem(**values)
