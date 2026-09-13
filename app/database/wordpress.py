"""MySQL/MariaDB discovery, backup, dry-run, and safe WordPress reference updates."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from app.core.jobs import utc_now
from app.database.jobs import JobRepository
from app.database.reference_transform import ChangeType, contains_reference, transform_value
from app.image.wordpress import WordPressTables, discover_wordpress_tables
from app.online.models import ReferenceChange, ReferenceUpdater

IDENTIFIER = re.compile(r"^[A-Za-z0-9_$]+$")
TEXT_TYPES = frozenset({"char", "varchar", "tinytext", "text", "mediumtext", "longtext", "json"})


@dataclass(frozen=True, slots=True)
class RuntimeDatabaseCredentials:
    username: str
    password: str = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    host: str
    database: str
    port: int = 3306
    charset: str = "utf8mb4"
    connect_timeout: int = 15


class ConnectionFactory(Protocol):
    def __call__(self): ...


def mysql_connection_factory(config: DatabaseConfig, credentials: RuntimeDatabaseCredentials) -> ConnectionFactory:
    """Create connections without persisting or interpolating authentication material."""
    def connect():
        import pymysql
        return pymysql.connect(host=config.host, port=config.port, user=credentials.username,
            password=credentials.password, database=config.database, charset=config.charset,
            connect_timeout=config.connect_timeout, autocommit=False,
            cursorclass=pymysql.cursors.SSCursor)
    return connect


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    name: str
    data_type: str
    primary: bool = False


@dataclass(frozen=True, slots=True)
class ReferenceRecord:
    id: int
    table_name: str
    primary_key_column: str
    record_id: str
    column_name: str
    old_value: str
    new_value: str | None
    change_type: str
    status: str
    review_reason: str | None


class WordPressChangeRepository:
    def __init__(self, jobs: JobRepository) -> None: self.jobs = jobs

    def save(self, job_id: str, table: str, primary: str, record_id: object, column: str,
             old: str, new: str | None, kind: ChangeType, status: str, reason: str | None) -> None:
        with self.jobs.connection() as connection:
            connection.execute("""INSERT INTO database_reference_changes(
                job_id,table_name,primary_key_column,record_id,column_name,old_value,new_value,
                change_type,status,review_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,table_name,primary_key_column,record_id,column_name) DO UPDATE SET
                old_value=excluded.old_value,new_value=excluded.new_value,change_type=excluded.change_type,
                status=excluded.status,review_reason=excluded.review_reason""",
                (job_id, table, primary, str(record_id), column, old, new, kind.value, status, reason, utc_now()))

    def edge(self, job_id: str, image: str, table: str, record_id: object, column: str, relation: str) -> None:
        with self.jobs.connection() as connection:
            connection.execute("""INSERT OR IGNORE INTO reference_edges(
                job_id,image_path,table_name,record_id,column_name,relation_type) VALUES(?,?,?,?,?,?)""",
                (job_id, image, table, str(record_id), column, relation))

    def iter_records(self, job_id: str, statuses: tuple[str, ...] = ("PROPOSED",), batch_size: int = 200) -> Iterator[ReferenceRecord]:
        after = 0
        while True:
            marks = ",".join("?" for _ in statuses)
            with self.jobs.connection() as connection:
                rows = connection.execute(f"""SELECT * FROM database_reference_changes
                    WHERE job_id=? AND id>? AND status IN ({marks}) ORDER BY id LIMIT ?""",
                    (job_id, after, *statuses, batch_size)).fetchall()
            if not rows: return
            for row in rows:
                yield ReferenceRecord(*(row[key] for key in ("id","table_name","primary_key_column","record_id",
                    "column_name","old_value","new_value","change_type","status","review_reason")))
            after = rows[-1]["id"]

    def set_status(self, record_id: int, status: str, reason: str | None = None) -> None:
        with self.jobs.connection() as connection:
            connection.execute("UPDATE database_reference_changes SET status=?,review_reason=COALESCE(?,review_reason) WHERE id=?",
                               (status, reason, record_id))

    def set_all_status(self, job_id: str, from_status: str, status: str) -> None:
        with self.jobs.connection() as connection:
            connection.execute("UPDATE database_reference_changes SET status=? WHERE job_id=? AND status=?",
                               (status, job_id, from_status))

    def summary(self, job_id: str) -> dict[str, int]:
        with self.jobs.connection() as connection:
            return {row[0]: int(row[1]) for row in connection.execute(
                "SELECT status,COUNT(*) FROM database_reference_changes WHERE job_id=? GROUP BY status", (job_id,))}


class LogicalDatabaseBackup:
    """Streaming, verified logical backup independent of external command-line tools."""

    def __init__(self, factory: ConnectionFactory, project_data: Path) -> None:
        self.factory, self.project_data = factory, project_data

    def create(self, job_id: str, tables: tuple[str, ...]) -> Path:
        directory = self.project_data / "jobs" / job_id / "database"; directory.mkdir(parents=True, exist_ok=True)
        path = directory / "wordpress-full-backup.jsonl.gz"
        connection = self.factory()
        try:
            snapshot = connection.cursor()
            snapshot.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            snapshot.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            with gzip.open(path, "wt", encoding="utf-8", newline="\n") as output:
                output.write(json.dumps({"format": 1, "created_at": utc_now(), "tables": tables}) + "\n")
                cursor = connection.cursor()
                for table in tables:
                    quoted = _quote(table)
                    cursor.execute(f"SHOW CREATE TABLE {quoted}"); create_row = cursor.fetchone()
                    output.write(json.dumps({"table": table, "create": create_row[1]}) + "\n")
                    cursor.execute(f"SELECT * FROM {quoted}")
                    columns = tuple(description[0] for description in cursor.description)
                    while rows := cursor.fetchmany(500):
                        for row in rows:
                            output.write(json.dumps({"table": table, "columns": columns,
                                "values": [_encode(value) for value in row]}, ensure_ascii=False) + "\n")
            connection.rollback()  # End the read-only consistent snapshot.
        finally: connection.close()
        digest = _file_checksum(path); path.with_suffix(path.suffix + ".sha256").write_text(digest + "\n", encoding="ascii")
        self.verify(path)
        return path

    @staticmethod
    def verify(path: Path) -> None:
        expected = path.with_suffix(path.suffix + ".sha256").read_text(encoding="ascii").strip()
        if _file_checksum(path) != expected: raise IOError("Database backup checksum verification failed.")
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            header = json.loads(next(stream));
            if header.get("format") != 1: raise IOError("Unsupported database backup format.")
            for line in stream: json.loads(line)

    def restore(self, path: Path) -> None:
        """Restore every backed-up table after a failed multi-batch mutation."""
        self.verify(path)
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            header = json.loads(next(stream)); tables = tuple(header["tables"])
            connection = self.factory()
            try:
                cursor = connection.cursor(); cursor.execute("SET FOREIGN_KEY_CHECKS=0")
                for table in reversed(tables): cursor.execute(f"DELETE FROM {_quote(table)}")
                for line in stream:
                    record = json.loads(line)
                    if "values" not in record: continue
                    columns = tuple(record["columns"]); values = tuple(_decode(v) for v in record["values"])
                    cursor.execute(f"INSERT INTO {_quote(record['table'])} "
                        f"({','.join(_quote(c) for c in columns)}) VALUES ({','.join('%s' for _ in values)})", values)
                cursor.execute("SET FOREIGN_KEY_CHECKS=1"); connection.commit()
            except Exception:
                connection.rollback(); raise
            finally: connection.close()


class WordPressReferenceUpdater(ReferenceUpdater):
    """Conservative dry-run-first updater with optimistic writes and verification."""

    def __init__(self, job_id: str, factory: ConnectionFactory, project_data: Path,
                 jobs: JobRepository, *, batch_size: int = 100) -> None:
        self.job_id, self.factory, self.project_data = job_id, factory, project_data
        self.repository = WordPressChangeRepository(jobs); self.batch_size = max(1, batch_size)
        self.tables: WordPressTables | None = None; self.backup_path: Path | None = None
        self._approved = False

    def discover(self) -> WordPressTables:
        connection = self.factory()
        try:
            cursor = connection.cursor(); cursor.execute("SHOW TABLES")
            names = tuple(str(row[0]) for row in cursor.fetchall())
        finally: connection.close()
        tables = discover_wordpress_tables(names)
        if not tables: raise RuntimeError("WordPress table prefix could not be discovered.")
        self.tables = tables; return tables

    def prepare(self, changes: tuple[ReferenceChange, ...]) -> None:
        """Create and verify a full backup, then persist a mutation-free dry run."""
        expected = self.project_data / "jobs" / self.job_id / "database" / "wordpress-full-backup.jsonl.gz"
        if expected.exists():
            LogicalDatabaseBackup.verify(expected); self.backup_path = expected
        if self.repository.summary(self.job_id).get("PROPOSED", 0):
            return  # Preserve explicit approval when the coordinator resumes this reviewed batch.
        self._approved = False
        tables = self.discover(); all_names = self._table_names(); names = tuple(n for n in all_names if n.startswith(tables.prefix))
        if not self.backup_path:
            self.backup_path = LogicalDatabaseBackup(self.factory, self.project_data).create(self.job_id, all_names)
        connection = self.factory()
        try:
            for table in names: self._inventory_table(connection, table, changes)
        finally: connection.close()

    def apply(self, changes: tuple[ReferenceChange, ...]) -> None:
        if not self.backup_path: raise RuntimeError("A verified database backup and dry run are required.")
        if not self._approved: raise PermissionError("Database changes require explicit approval after reviewing the dry run.")
        LogicalDatabaseBackup.verify(self.backup_path)
        iterator = self.repository.iter_records(self.job_id)
        try:
            while batch := tuple(_take(iterator, self.batch_size)):
                connection = self.factory()
                try:
                    cursor = connection.cursor()
                    for record in batch:
                        if record.new_value is None: continue
                        cursor.execute(f"UPDATE {_quote(record.table_name)} SET {_quote(record.column_name)}=%s "
                                       f"WHERE {_quote(record.primary_key_column)}=%s AND {_quote(record.column_name)}=%s",
                                       (record.new_value, record.record_id, record.old_value))
                        if cursor.rowcount != 1: raise RuntimeError("Database record changed after dry run; update aborted.")
                    connection.commit()
                except Exception:
                    connection.rollback(); raise
                finally: connection.close()
                for record in batch: self.repository.set_status(record.id, "UPDATED")
        except Exception:
            LogicalDatabaseBackup(self.factory, self.project_data).restore(self.backup_path)
            self.repository.set_all_status(self.job_id, "UPDATED", "ROLLED_BACK")
            raise

    def approve(self) -> None:
        """Authorize only the already-persisted and reviewable dry-run proposal."""
        if not self.backup_path: raise RuntimeError("Run prepare before approval.")
        if self.repository.summary(self.job_id).get("REVIEW", 0):
            raise PermissionError("Records requiring manual review prevent automatic approval.")
        self._approved = True

    def dry_run_records(self) -> Iterator[ReferenceRecord]:
        return self.repository.iter_records(self.job_id, ("PROPOSED", "REVIEW", "UPDATED"))

    def verify(self, changes: tuple[ReferenceChange, ...]) -> bool:
        connection = self.factory()
        try:
            for record in self.repository.iter_records(self.job_id, ("UPDATED",)):
                cursor = connection.cursor(); cursor.execute(
                    f"SELECT {_quote(record.column_name)} FROM {_quote(record.table_name)} WHERE {_quote(record.primary_key_column)}=%s",
                    (record.record_id,)); row = cursor.fetchone()
                if not row or row[0] != record.new_value: return False
                result = transform_value(str(row[0]), changes,
                    html=record.table_name.endswith("posts") and record.column_name == "post_content")
                if result.change_type is ChangeType.REVIEW: return False
                if any(contains_reference(str(row[0]), change.original_path) for change in changes): return False
            return not self._references_exist(connection, tuple(change.original_path for change in changes))
        finally: connection.close()

    def _references_exist(self, connection, paths: tuple[str, ...]) -> bool:
        if not self.tables: self.discover()
        for table in self._all_tables(self.tables.prefix):
            cursor = connection.cursor(); cursor.execute(f"SHOW COLUMNS FROM {_quote(table)}")
            columns = tuple(str(row[0]) for row in cursor.fetchall()
                            if str(row[1]).split("(", 1)[0].casefold() in TEXT_TYPES)
            for column in columns:
                cursor.execute(f"SELECT {_quote(column)} FROM {_quote(table)} WHERE {_quote(column)} IS NOT NULL")
                while rows := cursor.fetchmany(500):
                    if any(isinstance(row[0], str) and any(contains_reference(row[0], path) for path in paths)
                           for row in rows): return True
        return False

    def _all_tables(self, prefix: str) -> tuple[str, ...]:
        return tuple(name for name in self._table_names() if name.startswith(prefix))

    def _table_names(self) -> tuple[str, ...]:
        connection = self.factory()
        try:
            cursor = connection.cursor(); cursor.execute("SHOW TABLES")
            return tuple(sorted(str(row[0]) for row in cursor.fetchall()))
        finally: connection.close()

    def _inventory_table(self, connection, table: str, changes: tuple[ReferenceChange, ...]) -> None:
        cursor = connection.cursor(); cursor.execute(f"SHOW COLUMNS FROM {_quote(table)}")
        columns = tuple(ColumnInfo(str(r[0]), str(r[1]).split("(", 1)[0].casefold(), str(r[3]) == "PRI") for r in cursor.fetchall())
        primary = next((c for c in columns if c.primary), None)
        if not primary: return
        for column in (c for c in columns if c.data_type in TEXT_TYPES):
            cursor.execute(f"SELECT {_quote(primary.name)},{_quote(column.name)} FROM {_quote(table)} WHERE {_quote(column.name)} IS NOT NULL")
            while rows := cursor.fetchmany(500):
                for record_id, raw in rows:
                    if not isinstance(raw, str) or not any(contains_reference(raw, c.original_path) for c in changes): continue
                    html = table.endswith("posts") and column.name in {"post_content", "post_excerpt"}
                    result = transform_value(raw, changes, html=html)
                    status = "REVIEW" if result.change_type is ChangeType.REVIEW else "PROPOSED" if result.changed else "UNCHANGED"
                    self.repository.save(self.job_id, table, primary.name, record_id, column.name, raw,
                                         result.value if result.changed else None, result.change_type, status, result.review_reason)
                    for change in changes:
                        if contains_reference(raw, change.original_path):
                            relation = "ATTACHMENT_METADATA" if "attachment_metadata" in raw or column.name == "meta_value" else "CONTENT"
                            self.repository.edge(self.job_id, change.original_path, table, record_id, column.name, relation)


def _quote(identifier: str) -> str:
    if not IDENTIFIER.fullmatch(identifier): raise ValueError("Unsafe database identifier.")
    return f"`{identifier}`"


def _encode(value: Any) -> Any:
    if isinstance(value, bytes): return {"__bytes__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Decimal): return {"__decimal__": str(value)}
    if isinstance(value, (datetime, date, time)): return {"__temporal__": type(value).__name__, "value": value.isoformat()}
    if value is None or isinstance(value, (str, int, float, bool)): return value
    raise TypeError(f"Unsupported database backup value type: {type(value).__name__}")


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"__bytes__"}: return base64.b64decode(value["__bytes__"])
    if isinstance(value, dict) and set(value) == {"__decimal__"}: return Decimal(value["__decimal__"])
    if isinstance(value, dict) and set(value) == {"__temporal__", "value"}:
        return {"datetime": datetime, "date": date, "time": time}[value["__temporal__"]].fromisoformat(value["value"])
    return value


def _file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024): digest.update(chunk)
    return digest.hexdigest()


def _take(iterator, limit):
    for _ in range(limit):
        try: yield next(iterator)
        except StopIteration: return
