from __future__ import annotations

import sqlite3

import pytest

from app.core.engine import JobEngine
from app.core.resources import ResourceLimitExceeded, ResourceLimits, ResourceManager
from app.database.jobs import DATABASE_SCHEMA_VERSION, JobRepository
from app.database.optimization import OptimizationRepository
from app.image.optimization_models import OptimizationConfig, OptimizationDecision, OptimizationResult
from app.image.optimizer import OfflineOptimizer


def test_resource_manager_rejects_enormous_images():
    manager = ResourceManager(ResourceLimits(max_workers=1, max_pixels_per_image=100))
    with pytest.raises(ResourceLimitExceeded):
        with manager.reserve(101):
            pass


def test_corrupted_image_is_retained_without_candidate(tmp_path):
    source = tmp_path / "broken.jpg"
    source.write_bytes(b"definitely not a JPEG")
    result = OfflineOptimizer(tmp_path).optimize("job", source, "/uploads/broken.jpg")
    assert result.decision is OptimizationDecision.FAILED
    assert result.candidate_path is None and source.exists()


def test_oversized_file_is_safely_retained(tmp_path):
    source = tmp_path / "large.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 128)
    config = OptimizationConfig(max_file_bytes=32)
    result = OfflineOptimizer(tmp_path, config=config).optimize("job", source, "/uploads/large.png")
    assert result.decision is OptimizationDecision.RETAINED_ORIGINAL
    assert "byte limit" in result.decision_reason


def test_svg_metadata_cleanup_never_overwrites_original(tmp_path):
    source = tmp_path / "logo.svg"
    source.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
        f'<metadata>{"editor-data" * 100}</metadata><rect width="20" height="10"/></svg>',
        encoding="utf-8",
    )
    before = source.read_bytes()
    config = OptimizationConfig(minimum_savings_bytes=1, minimum_savings_ratio=0)
    result = OfflineOptimizer(tmp_path, config=config).optimize("job", source, "/uploads/logo.svg")
    assert result.decision is OptimizationDecision.SELECTED
    assert source.read_bytes() == before
    assert result.candidate_path != str(source)
    assert b"metadata" not in open(result.candidate_path, "rb").read()


def test_suspicious_svg_is_not_rewritten(tmp_path):
    source = tmp_path / "unsafe.svg"
    source.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', encoding="utf-8")
    result = OfflineOptimizer(tmp_path).optimize("job", source, "/uploads/unsafe.svg")
    assert result.decision is OptimizationDecision.FAILED
    assert not (tmp_path / "jobs" / "job" / "processed").exists()


def test_optimization_result_round_trip_and_schema_migration(tmp_path):
    database = tmp_path / "state.db"
    jobs = JobRepository(database)
    jobs.initialize()
    job = JobEngine(jobs).create_job("example.com")
    repository = OptimizationRepository(jobs)
    expected = OptimizationResult(
        job.id, "/uploads/a.jpg", 2000, OptimizationDecision.SELECTED, "validated",
        "/local/a.webp", "WEBP", 1000, {"quality": 82}, 100, 50,
        False, None, False, "abc", True, "Validated", 1000, .5,
    )
    repository.save(expected)
    assert repository.get(job.id, expected.original_path) == expected
    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='optimization_results'"
        ).fetchone()
    assert version == DATABASE_SCHEMA_VERSION == 3 and table


def test_version_two_database_migrates_to_results_table(tmp_path):
    database = tmp_path / "v2.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=2")
    JobRepository(database).initialize()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='optimization_results'"
        ).fetchone()
