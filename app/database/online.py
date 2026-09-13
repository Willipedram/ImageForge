"""Paged SQLite persistence for online manifests."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from app.core.jobs import utc_now
from app.database.jobs import JobRepository
from app.online.models import OnlineItem, OnlineItemStatus, OnlineReport


class OnlineRepository:
    def __init__(self, jobs: JobRepository) -> None:
        self.jobs = jobs

    def create_run(self, job_id: str, remote_root: str, staging_root: str) -> None:
        now = utc_now()
        with self.jobs.connection() as connection:
            connection.execute(
                "INSERT INTO online_runs(job_id,remote_root,staging_root,created_at,updated_at) VALUES(?,?,?,?,?)",
                (job_id, remote_root, staging_root, now, now),
            )

    def run(self, job_id: str) -> dict | None:
        with self.jobs.connection() as connection:
            row = connection.execute("SELECT * FROM online_runs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def set_discovery(self, job_id: str, site_root: str, uploads_root: str) -> None:
        with self.jobs.connection() as connection:
            connection.execute(
                "UPDATE online_runs SET site_root=?,uploads_root=?,updated_at=? WHERE job_id=?",
                (site_root, uploads_root, utc_now(), job_id),
            )

    def save_items(self, items: Iterable[OnlineItem]) -> int:
        rows = [self._values(item) for item in items]
        if not rows:
            return 0
        with self.jobs.connection() as connection:
            connection.executemany(self._upsert_sql(), rows)
        return len(rows)

    def save_item(self, item: OnlineItem) -> None:
        with self.jobs.connection() as connection:
            connection.execute(self._upsert_sql(), self._values(item))

    def iter_items(self, job_id: str, statuses: set[OnlineItemStatus] | None = None,
                   batch_size: int = 200) -> Iterator[OnlineItem]:
        after = ""
        while True:
            parameters: list[object] = [job_id, after]
            query = "SELECT * FROM online_items WHERE job_id=? AND id>?"
            if statuses:
                values = sorted(status.value for status in statuses)
                query += f" AND status IN ({','.join('?' for _ in values)})"
                parameters.extend(values)
            query += " ORDER BY id LIMIT ?"
            parameters.append(batch_size)
            with self.jobs.connection() as connection:
                rows = connection.execute(query, parameters).fetchall()
            if not rows:
                return
            for row in rows:
                yield self._item(row)
            after = rows[-1]["id"]

    def count(self, job_id: str) -> int:
        with self.jobs.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM online_items WHERE job_id=?", (job_id,)).fetchone()[0])

    def count_statuses(self, job_id: str, statuses: set[OnlineItemStatus]) -> int:
        values = sorted(status.value for status in statuses)
        with self.jobs.connection() as connection:
            return int(connection.execute(
                f"SELECT COUNT(*) FROM online_items WHERE job_id=? AND status IN ({','.join('?' for _ in values)})",
                (job_id, *values),
            ).fetchone()[0])

    def report(self, job_id: str) -> OnlineReport:
        with self.jobs.connection() as connection:
            row = connection.execute(
                """SELECT COUNT(*) total,
                SUM(status='FINAL_VERIFIED') completed,SUM(status='SKIPPED') skipped,SUM(status='FAILED') failed,
                COALESCE(SUM(original_bytes),0) originals,
                COALESCE(SUM(CASE WHEN decision='SELECTED' THEN candidate_bytes ELSE 0 END),0) candidates,
                COALESCE(SUM(CASE WHEN local_path IS NOT NULL THEN original_bytes ELSE 0 END),0) downloaded,
                COALESCE(SUM(CASE WHEN upload_status='UPLOADED' THEN candidate_bytes ELSE 0 END),0) uploaded,
                COALESCE(SUM(retry_count),0) retries FROM online_items WHERE job_id=?""", (job_id,),
            ).fetchone()
            errors = tuple(r[0] for r in connection.execute(
                "SELECT error FROM online_items WHERE job_id=? AND error IS NOT NULL ORDER BY remote_path", (job_id,)
            ))
        return OnlineReport(job_id, *(int(row[key] or 0) for key in
            ("total", "completed", "skipped", "failed", "originals", "candidates", "downloaded", "uploaded", "retries")), errors)

    @staticmethod
    def _upsert_sql() -> str:
        return """INSERT INTO online_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET status=excluded.status,original_checksum=excluded.original_checksum,
        local_path=excluded.local_path,local_checksum=excluded.local_checksum,candidate_path=excluded.candidate_path,
        candidate_format=excluded.candidate_format,candidate_bytes=excluded.candidate_bytes,
        candidate_checksum=excluded.candidate_checksum,production_path=excluded.production_path,
        staging_path=excluded.staging_path,decision=excluded.decision,decision_reason=excluded.decision_reason,
        upload_status=excluded.upload_status,verification_status=excluded.verification_status,
        database_status=excluded.database_status,retry_count=excluded.retry_count,error=excluded.error,
        updated_at=excluded.updated_at"""

    @staticmethod
    def _values(item: OnlineItem) -> tuple:
        return (item.id,item.job_id,item.remote_path,item.status.value,item.original_bytes,item.original_checksum,
            item.local_path,item.local_checksum,item.candidate_path,item.candidate_format,item.candidate_bytes,
            item.candidate_checksum,item.production_path,item.staging_path,item.decision,item.decision_reason,
            item.upload_status,item.verification_status,item.database_status,item.retry_count,item.error,utc_now())

    @staticmethod
    def _item(row) -> OnlineItem:
        data = dict(row); data.pop("updated_at")
        data["status"] = OnlineItemStatus(data["status"])
        return OnlineItem(**data)
