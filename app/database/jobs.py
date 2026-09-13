"""Transactional SQLite persistence for jobs, items, events and checkpoints."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.core.jobs import ItemStatus, Job, JobItem, JobStatus, UNFINISHED_JOB_STATES, utc_now

DATABASE_SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, target TEXT NOT NULL, created_at TEXT NOT NULL,
    started_at TEXT, ended_at TEXT, status TEXT NOT NULL, progress REAL NOT NULL,
    current_stage TEXT NOT NULL, resume_state TEXT, error TEXT,
    files_total INTEGER NOT NULL DEFAULT 0, files_completed INTEGER NOT NULL DEFAULT 0,
    original_bytes INTEGER NOT NULL DEFAULT 0, optimized_bytes INTEGER NOT NULL DEFAULT 0,
    saved_bytes INTEGER NOT NULL DEFAULT 0, duration_seconds REAL NOT NULL DEFAULT 0,
    application_version TEXT NOT NULL, data_schema_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS job_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    stage TEXT NOT NULL, entered_at TEXT NOT NULL, exited_at TEXT, outcome TEXT
);
CREATE TABLE IF NOT EXISTS job_items (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source TEXT NOT NULL, status TEXT NOT NULL, local_path TEXT, remote_path TEXT,
    original_bytes INTEGER NOT NULL DEFAULT 0, optimized_bytes INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0, last_error TEXT, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_job_status ON job_items(job_id, status);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    item_id TEXT, event_type TEXT NOT NULL, message TEXT NOT NULL, payload TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    item_id TEXT, classification TEXT NOT NULL, error_type TEXT NOT NULL,
    message TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    item_id TEXT, operation TEXT NOT NULL, state TEXT NOT NULL, payload TEXT,
    is_safe INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_job ON checkpoints(job_id, id DESC);
CREATE TABLE IF NOT EXISTS target_locks (
    target TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS image_inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    remote_path TEXT NOT NULL, filename TEXT NOT NULL, extension TEXT NOT NULL,
    detected_format TEXT, mime_type TEXT, size INTEGER NOT NULL,
    width INTEGER, height INTEGER, aspect_ratio REAL, color_mode TEXT,
    has_alpha INTEGER NOT NULL, transparency_ratio REAL, has_semitransparency INTEGER NOT NULL,
    is_animated INTEGER NOT NULL, frame_count INTEGER NOT NULL,
    has_exif INTEGER NOT NULL, orientation INTEGER, has_icc_profile INTEGER NOT NULL,
    checksum TEXT, attachment_id INTEGER, parent_path TEXT, derivative_kind TEXT,
    asset_class TEXT NOT NULL, suspicious INTEGER NOT NULL,
    unnecessary_metadata INTEGER NOT NULL, skip_reason TEXT, signature_valid INTEGER NOT NULL,
    UNIQUE(job_id, remote_path)
);
CREATE INDEX IF NOT EXISTS idx_inventory_job_format ON image_inventory(job_id, detected_format);
CREATE INDEX IF NOT EXISTS idx_inventory_job_class ON image_inventory(job_id, asset_class);
CREATE INDEX IF NOT EXISTS idx_inventory_job_parent ON image_inventory(job_id, parent_path);
CREATE TABLE IF NOT EXISTS optimization_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    original_path TEXT NOT NULL, original_bytes INTEGER NOT NULL,
    candidate_path TEXT, candidate_format TEXT, candidate_bytes INTEGER,
    quality_parameters TEXT NOT NULL, width INTEGER, height INTEGER,
    has_alpha INTEGER NOT NULL, transparency_ratio REAL, has_semitransparency INTEGER NOT NULL,
    checksum TEXT, validation_passed INTEGER NOT NULL, validation_reason TEXT NOT NULL,
    savings_bytes INTEGER NOT NULL, savings_ratio REAL NOT NULL,
    decision TEXT NOT NULL, decision_reason TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(job_id, original_path)
);
CREATE INDEX IF NOT EXISTS idx_optimization_job_decision ON optimization_results(job_id, decision);
"""

INVENTORY_SCHEMA = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS image_inventory"):]
OPTIMIZATION_SCHEMA = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS optimization_results"):]


class TargetLockedError(RuntimeError):
    pass


class JobRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > DATABASE_SCHEMA_VERSION:
                raise RuntimeError(f"Job database schema {version} is newer than supported {DATABASE_SCHEMA_VERSION}.")
            if version == 0:
                connection.executescript(SCHEMA)
                connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")
            elif version == 1:
                connection.executescript(INVENTORY_SCHEMA)
                connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")
            elif version == 2:
                connection.executescript(OPTIMIZATION_SCHEMA)
                connection.execute("PRAGMA user_version = 3")

    def save(self, job: Job, connection: sqlite3.Connection | None = None) -> None:
        job.validate()
        values = (
            job.id, job.target, job.created_at, job.started_at, job.ended_at, job.status.value,
            job.progress, job.current_stage, job.resume_state.value if job.resume_state else None,
            job.error, job.files_total, job.files_completed, job.original_bytes,
            job.optimized_bytes, job.saved_bytes, job.duration_seconds,
            job.application_version, job.data_schema_version, utc_now(),
        )
        sql = """INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET target=excluded.target, started_at=excluded.started_at,
            ended_at=excluded.ended_at, status=excluded.status, progress=excluded.progress,
            current_stage=excluded.current_stage, resume_state=excluded.resume_state,
            error=excluded.error, files_total=excluded.files_total,
            files_completed=excluded.files_completed, original_bytes=excluded.original_bytes,
            optimized_bytes=excluded.optimized_bytes, saved_bytes=excluded.saved_bytes,
            duration_seconds=excluded.duration_seconds, updated_at=excluded.updated_at"""
        if connection is not None:
            connection.execute(sql, values)
        else:
            with self.connection() as own_connection:
                own_connection.execute(sql, values)

    def get(self, job_id: str) -> Job | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def list_jobs(self, unfinished_only: bool = False) -> list[Job]:
        query = "SELECT * FROM jobs"
        parameters: tuple[Any, ...] = ()
        if unfinished_only:
            values = tuple(state.value for state in UNFINISHED_JOB_STATES)
            query += f" WHERE status IN ({','.join('?' for _ in values)})"
            parameters = values
        query += " ORDER BY created_at DESC"
        with self.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._job(row) for row in rows]

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"], target=row["target"], created_at=row["created_at"],
            started_at=row["started_at"], ended_at=row["ended_at"], status=JobStatus(row["status"]),
            progress=row["progress"], current_stage=row["current_stage"],
            resume_state=JobStatus(row["resume_state"]) if row["resume_state"] else None,
            error=row["error"], files_total=row["files_total"], files_completed=row["files_completed"],
            original_bytes=row["original_bytes"], optimized_bytes=row["optimized_bytes"],
            saved_bytes=row["saved_bytes"], duration_seconds=row["duration_seconds"],
            application_version=row["application_version"], data_schema_version=row["data_schema_version"],
        )

    def save_item(self, item: JobItem, connection: sqlite3.Connection | None = None) -> None:
        values = (
            item.id, item.job_id, item.source, item.status.value, item.local_path, item.remote_path,
            item.original_bytes, item.optimized_bytes, item.retry_count, item.last_error, item.updated_at,
        )
        sql = """INSERT INTO job_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET status=excluded.status, local_path=excluded.local_path,
            remote_path=excluded.remote_path, original_bytes=excluded.original_bytes,
            optimized_bytes=excluded.optimized_bytes, retry_count=excluded.retry_count,
            last_error=excluded.last_error, updated_at=excluded.updated_at"""
        if connection is not None:
            connection.execute(sql, values)
        else:
            with self.connection() as own_connection:
                own_connection.execute(sql, values)

    def list_items(self, job_id: str) -> list[JobItem]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM job_items WHERE job_id=? ORDER BY source", (job_id,)).fetchall()
        return [JobItem(**{**dict(row), "status": ItemStatus(row["status"])}) for row in rows]

    def event(self, connection: sqlite3.Connection, job_id: str, event_type: str, message: str,
              item_id: str | None = None, payload: dict[str, Any] | None = None) -> None:
        connection.execute(
            "INSERT INTO events(job_id,item_id,event_type,message,payload,created_at) VALUES(?,?,?,?,?,?)",
            (job_id, item_id, event_type, message, json.dumps(payload) if payload else None, utc_now()),
        )

    def checkpoint(self, connection: sqlite3.Connection, job_id: str, operation: str, state: str,
                   item_id: str | None = None, payload: dict[str, Any] | None = None,
                   is_safe: bool = True) -> int:
        cursor = connection.execute(
            "INSERT INTO checkpoints(job_id,item_id,operation,state,payload,is_safe,created_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, item_id, operation, state, json.dumps(payload) if payload else None, int(is_safe), utc_now()),
        )
        return int(cursor.lastrowid)

    def last_safe_checkpoint(self, job_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE job_id=? AND is_safe=1 ORDER BY id DESC LIMIT 1", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def acquire_lock(self, connection: sqlite3.Connection, target: str, job_id: str) -> None:
        try:
            connection.execute("INSERT INTO target_locks VALUES(?,?,?)", (target, job_id, utc_now()))
        except sqlite3.IntegrityError as exc:
            raise TargetLockedError("Another optimization job is currently active for this target.") from exc

    def release_lock(self, connection: sqlite3.Connection, job_id: str) -> None:
        connection.execute("DELETE FROM target_locks WHERE job_id=?", (job_id,))
