from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from app.core.engine import JobEngine
from app.database.jobs import DATABASE_SCHEMA_VERSION, JobRepository
from app.database.reference_transform import ChangeType, transform_value
from app.database.wordpress import LogicalDatabaseBackup, WordPressChangeRepository, WordPressReferenceUpdater
from app.database.wordpress_serialization import PHPArray, PHPSerializationError, dumps, loads
from app.online.models import ReferenceChange

OLD = "/wp-content/uploads/2026/image.jpg"
NEW = "/wp-content/uploads/2026/image.webp"
CHANGES = (ReferenceChange(OLD, NEW),)


def test_php_serialization_updates_utf8_byte_lengths_and_nested_arrays():
    value = PHPArray((("file", "2026/图像.jpg"), ("url", OLD), ("empty", None),
                      ("sizes", PHPArray(((0, OLD),))),))
    encoded = dumps(value)
    result = transform_value(encoded, CHANGES)
    assert result.changed and result.change_type is ChangeType.PHP_SERIALIZED
    decoded = loads(result.value)
    assert isinstance(decoded, PHPArray) and NEW in result.value
    assert dumps(decoded) == result.value


def test_malformed_or_object_serialization_is_review_only():
    malformed = 'a:1:{s:3:"url";s:2:"too long";}'
    result = transform_value(malformed, CHANGES)
    assert not result.changed and result.change_type is ChangeType.REVIEW
    assert transform_value('O:8:"stdClass":0:{} ' + OLD, CHANGES).change_type is ChangeType.REVIEW


@pytest.mark.parametrize("value,kind", [
    ('{"url":"https://example.test/wp-content/uploads/2026/image.jpg","nested":["' + OLD + '"]}', ChangeType.JSON),
    ('<img src="https://example.test' + OLD + '"><p>' + OLD + '</p>', ChangeType.HTML),
    ('https://example.test' + OLD + '?size=large', ChangeType.PLAIN),
    ('https:\\/\\/example.test\\/wp-content\\/uploads\\/2026\\/image.jpg', ChangeType.PLAIN),
    ('2026/image.jpg', ChangeType.PLAIN),
])
def test_structured_and_url_forms(value, kind):
    result = transform_value(value, CHANGES, html=kind is ChangeType.HTML)
    assert result.changed and result.change_type is kind
    if kind is ChangeType.HTML:
        assert '<p>' + OLD + '</p>' in result.value  # unrelated text nodes are untouched
        assert 'image.webp' in result.value
    else:
        assert "image.webp" in result.value


class Cursor:
    def __init__(self, database): self.db = database; self.rows = []; self.description = (); self.rowcount = 0
    def execute(self, sql, params=()):
        normalized = " ".join(sql.split()); self.rowcount = 0
        if normalized.startswith("SET FOREIGN_KEY_CHECKS"): self.rows = []
        elif normalized == "SHOW TABLES": self.rows = [(name,) for name in self.db.tables]
        elif normalized.startswith("SHOW CREATE TABLE"):
            table = _table(sql); self.rows = [(table, f"CREATE TABLE `{table}` (`id` bigint primary key, `value` longtext)")]
        elif normalized.startswith("SHOW COLUMNS"):
            table = _table(sql); self.rows = [(name, typ, "NO", "PRI" if primary else "") for name, typ, primary in self.db.columns[table]]
        elif normalized.startswith("SELECT *"):
            table = _table(sql); names = [c[0] for c in self.db.columns[table]]
            self.description = tuple((name,) for name in names); self.rows = [tuple(row[n] for n in names) for row in self.db.rows[table]]
        elif normalized.startswith("SELECT") and " IS NOT NULL" in normalized:
            table = _table(sql); selected = [part.strip(" `") for part in normalized.split(" FROM ")[0][7:].split(",")]
            self.rows = [tuple(row[name] for name in selected) for row in self.db.rows[table]
                         if row[selected[-1]] is not None]
        elif normalized.startswith("SELECT"):
            table = _table(sql); column = normalized.split("SELECT `",1)[1].split("`",1)[0]
            pk = normalized.split("WHERE `",1)[1].split("`",1)[0]
            self.rows = [(row[column],) for row in self.db.rows[table] if str(row[pk]) == str(params[0])]
        elif normalized.startswith("UPDATE"):
            self.db.updates += 1
            if self.db.fail_update_at == self.db.updates: raise RuntimeError("simulated write failure")
            table = _table(sql); column = normalized.split(" SET `",1)[1].split("`",1)[0]
            pk = normalized.split("WHERE `",1)[1].split("`",1)[0]; new, identity, old = params
            for row in self.db.rows[table]:
                if str(row[pk]) == str(identity) and row[column] == old: row[column] = new; self.rowcount = 1
        elif normalized.startswith("DELETE FROM"):
            self.db.rows[_table(sql)] = []
        elif normalized.startswith("INSERT INTO"):
            table = _table(sql); columns = tuple(normalized.split("(",1)[1].split(")",1)[0].replace("`", "").split(","))
            self.db.rows[table].append(dict(zip(columns, params))); self.rowcount = 1
        return self
    def fetchall(self): result, self.rows = self.rows, []; return result
    def fetchone(self): return self.rows.pop(0) if self.rows else None
    def fetchmany(self, size): result, self.rows = self.rows[:size], self.rows[size:]; return result


class Connection:
    def __init__(self, database): self.db = database
    def cursor(self): return Cursor(self.db)
    def commit(self): self.db.commits += 1
    def rollback(self): self.db.rollbacks += 1
    def close(self): pass


class FakeMySQL:
    def __init__(self):
        self.tables = ("acme_posts", "acme_postmeta", "acme_options", "acme_termmeta", "acme_woocommerce_data", "other_table")
        self.columns = {name: [("id", "bigint", True), ("value", "longtext", False)] for name in self.tables}
        self.rows = {name: [] for name in self.tables}; self.commits = self.rollbacks = self.updates = 0
        self.fail_update_at = -1
    def connect(self): return Connection(self)


def _table(sql):
    candidates = [part for part in sql.split("`") if part.startswith(("acme_", "other_"))]
    return candidates[0]


def test_dry_run_backup_inventory_apply_verify_and_reference_graph(tmp_path):
    mysql = FakeMySQL()
    mysql.rows["acme_posts"] = [{"id": 1, "value": '<img src="https://shop.test' + OLD + '">'}]
    serialized = dumps(PHPArray((("file", OLD), ("sizes", PHPArray((("thumbnail", OLD),))),)))
    mysql.rows["acme_postmeta"] = [{"id": 9, "value": serialized}]
    mysql.rows["acme_options"] = [{"id": 3, "value": json.dumps({"elementor": {"background": OLD}})}]
    mysql.rows["acme_woocommerce_data"] = [{"id": 7, "value": OLD}]
    data = tmp_path / "ProjectData"; jobs = JobRepository(data / "jobs" / "state.db"); jobs.initialize()
    job = JobEngine(jobs).create_job("remote:shop.test")
    updater = WordPressReferenceUpdater(job.id, mysql.connect, data, jobs, batch_size=2)
    updater.prepare(CHANGES)
    summary = updater.repository.summary(job.id)
    assert summary == {"PROPOSED": 4}
    assert all(OLD in row["value"] for table in mysql.rows.values() for row in table)
    assert updater.tables.prefix == "acme_" and "acme_woocommerce_data" in updater._all_tables("acme_")
    assert updater.backup_path.parent == data / "jobs" / job.id / "database"
    LogicalDatabaseBackup.verify(updater.backup_path)
    with gzip.open(updater.backup_path, "rt", encoding="utf-8") as stream:
        assert json.loads(next(stream))["format"] == 1
    with pytest.raises(PermissionError): updater.apply(CHANGES)
    updater.approve()
    updater.apply(CHANGES)
    assert updater.verify(CHANGES)
    assert updater.repository.summary(job.id) == {"UPDATED": 4}
    assert mysql.commits == 2  # controlled batches, not one enormous transaction
    with jobs.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM reference_edges WHERE job_id=?", (job.id,)).fetchone()[0] == 4


def test_review_record_is_never_mutated(tmp_path):
    mysql = FakeMySQL(); unsafe = 'O:8:"stdClass":0:{}' + OLD
    mysql.rows["acme_postmeta"] = [{"id": 1, "value": unsafe}]
    data = tmp_path / "ProjectData"; jobs = JobRepository(data / "jobs" / "state.db"); jobs.initialize()
    job = JobEngine(jobs).create_job("remote:review.test")
    updater = WordPressReferenceUpdater(job.id, mysql.connect, data, jobs)
    updater.prepare(CHANGES)
    assert updater.repository.summary(job.id) == {"REVIEW": 1}
    with pytest.raises(PermissionError): updater.approve()
    with pytest.raises(PermissionError): updater.apply(CHANGES)
    assert mysql.rows["acme_postmeta"][0]["value"] == unsafe


def test_failed_later_batch_restores_verified_full_backup(tmp_path):
    mysql = FakeMySQL()
    mysql.rows["acme_posts"] = [{"id": 1, "value": OLD}, {"id": 2, "value": "https://shop.test" + OLD}]
    mysql.rows["other_table"] = [{"id": 99, "value": "must survive full restore"}]
    before = {table: [dict(row) for row in rows] for table, rows in mysql.rows.items()}
    data = tmp_path / "ProjectData"; jobs = JobRepository(data / "jobs" / "state.db"); jobs.initialize()
    job = JobEngine(jobs).create_job("remote:rollback.test")
    updater = WordPressReferenceUpdater(job.id, mysql.connect, data, jobs, batch_size=1)
    updater.prepare(CHANGES); updater.approve(); mysql.fail_update_at = 2
    with pytest.raises(RuntimeError, match="simulated write failure"): updater.apply(CHANGES)
    assert mysql.rows == before
    assert updater.repository.summary(job.id) == {"PROPOSED": 1, "ROLLED_BACK": 1}


def test_schema_migrates_six_to_reference_tables(tmp_path):
    path = tmp_path / "v6.db"
    with sqlite3.connect(path) as connection: connection.execute("PRAGMA user_version=6")
    JobRepository(path).initialize()
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert {"database_reference_changes", "reference_edges"} <= tables
    assert version == DATABASE_SCHEMA_VERSION == 8
