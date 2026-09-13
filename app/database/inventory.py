"""Paged SQLite storage and aggregation for image intelligence records."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from app.database.jobs import JobRepository
from app.image.models import AssetClass, ImageRecord


@dataclass(frozen=True, slots=True)
class InventorySummary:
    total_images: int
    total_bytes: int
    formats: dict[str, int]
    transparent: int
    animated: int
    derivatives: int
    suspicious: int
    skipped: int


@dataclass(frozen=True, slots=True)
class BrowserImage:
    filename: str
    original_path: str
    selected_path: str | None
    format: str
    original_bytes: int
    final_bytes: int
    savings_bytes: int
    decision: str
    confidence: str
    status: str


class InventoryRepository:
    COLUMNS = (
        "job_id", "remote_path", "filename", "extension", "detected_format", "mime_type", "size",
        "width", "height", "aspect_ratio", "color_mode", "has_alpha", "transparency_ratio",
        "has_semitransparency", "is_animated", "frame_count", "has_exif", "orientation",
        "has_icc_profile", "checksum", "attachment_id", "parent_path", "derivative_kind",
        "asset_class", "suspicious", "unnecessary_metadata", "skip_reason", "signature_valid",
    )

    def __init__(self, jobs: JobRepository) -> None:
        self.jobs = jobs

    def save_batch(self, records: Iterable[ImageRecord]) -> int:
        rows = [self._values(record) for record in records]
        if not rows:
            return 0
        placeholders = ",".join("?" for _ in self.COLUMNS)
        updates = ",".join(f"{column}=excluded.{column}" for column in self.COLUMNS[2:])
        with self.jobs.connection() as connection:
            connection.executemany(
                f"INSERT INTO image_inventory({','.join(self.COLUMNS)}) VALUES({placeholders}) "
                f"ON CONFLICT(job_id,remote_path) DO UPDATE SET {updates}", rows,
            )
        return len(rows)

    @staticmethod
    def _values(record: ImageRecord) -> tuple[Any, ...]:
        return (
            record.job_id, record.remote_path, record.filename, record.extension, record.detected_format,
            record.mime_type, record.size, record.width, record.height, record.aspect_ratio,
            record.color_mode, record.has_alpha, record.transparency_ratio, record.has_semitransparency,
            record.is_animated, record.frame_count, record.has_exif, record.orientation,
            record.has_icc_profile, record.checksum, record.attachment_id, record.parent_path,
            record.derivative_kind, record.asset_class.value, record.suspicious,
            record.unnecessary_metadata, record.skip_reason, record.signature_valid,
        )

    def iter_records(self, job_id: str, *, filter_name: str = "all", page_size: int = 500) -> Iterator[ImageRecord]:
        filters = {
            "all": ("1=1", ()), "transparent": ("has_alpha=1", ()),
            "animated": ("is_animated=1", ()), "derivatives": ("parent_path IS NOT NULL", ()),
            "suspicious": ("suspicious=1", ()), "skipped": ("skip_reason IS NOT NULL", ()),
        }
        if filter_name.startswith("format:"):
            where, parameters = "detected_format=?", (filter_name.split(":", 1)[1].upper(),)
        else:
            where, parameters = filters.get(filter_name, filters["all"])
        last_id = 0
        while True:
            with self.jobs.connection() as connection:
                rows = connection.execute(
                    f"SELECT * FROM image_inventory WHERE job_id=? AND id>? AND {where} ORDER BY id LIMIT ?",
                    (job_id, last_id, *parameters, page_size),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                last_id = row["id"]
                yield self._record(row)

    def get_by_path(self, job_id: str, remote_path: str) -> ImageRecord | None:
        with self.jobs.connection() as connection:
            row = connection.execute(
                "SELECT * FROM image_inventory WHERE job_id=? AND remote_path=?", (job_id, remote_path)
            ).fetchone()
        return self._record(row) if row else None

    def set_relationship(self, job_id: str, remote_path: str, parent_path: str,
                         kind: str, attachment_id: int | None = None) -> None:
        with self.jobs.connection() as connection:
            connection.execute(
                "UPDATE image_inventory SET parent_path=?, derivative_kind=?, attachment_id=COALESCE(?,attachment_id), "
                "asset_class=? WHERE job_id=? AND remote_path=?",
                (parent_path, kind, attachment_id, AssetClass.THUMBNAIL.value, job_id, remote_path),
            )

    def set_attachment(self, job_id: str, remote_path: str, attachment_id: int) -> None:
        with self.jobs.connection() as connection:
            connection.execute(
                "UPDATE image_inventory SET attachment_id=? WHERE job_id=? AND remote_path=?",
                (attachment_id, job_id, remote_path),
            )

    def summary(self, job_id: str) -> InventorySummary:
        with self.jobs.connection() as connection:
            totals = connection.execute(
                """SELECT COUNT(*), COALESCE(SUM(size),0), COALESCE(SUM(has_alpha),0),
                COALESCE(SUM(is_animated),0), SUM(parent_path IS NOT NULL), COALESCE(SUM(suspicious),0),
                SUM(skip_reason IS NOT NULL) FROM image_inventory WHERE job_id=?""", (job_id,),
            ).fetchone()
            formats = dict(connection.execute(
                "SELECT COALESCE(detected_format,'UNKNOWN'),COUNT(*) FROM image_inventory WHERE job_id=? GROUP BY detected_format",
                (job_id,),
            ).fetchall())
        values = [int(value or 0) for value in totals]
        return InventorySummary(values[0], values[1], formats, *values[2:])

    def browser_page(self, job_id: str, *, filter_name: str = "all", limit: int = 250) -> tuple[BrowserImage, ...]:
        where, parameters = "1=1", []
        if filter_name.startswith("format:"):
            where, parameters = "inventory.detected_format=?", [filter_name.split(":", 1)[1].upper()]
        elif filter_name == "optimized": where = "results.decision='SELECTED'"
        elif filter_name == "skipped": where = "COALESCE(results.decision,'SKIPPED')!='SELECTED'"
        elif filter_name == "transparent": where = "inventory.has_alpha=1"
        elif filter_name == "animated": where = "inventory.is_animated=1"
        elif filter_name == "derivatives": where = "inventory.parent_path IS NOT NULL"
        elif filter_name == "suspicious": where = "inventory.suspicious=1"
        with self.jobs.connection() as connection:
            rows = connection.execute(f"""SELECT inventory.filename,inventory.remote_path,
                results.candidate_path,COALESCE(results.candidate_format,inventory.detected_format,'UNKNOWN') format,
                inventory.size,COALESCE(results.candidate_bytes,inventory.size) final_bytes,
                COALESCE(results.savings_bytes,0) savings_bytes,COALESCE(results.decision,'PENDING') decision,
                COALESCE(results.confidence,'—') confidence,COALESCE(results.validation_reason,inventory.skip_reason,'Ready') status
                FROM image_inventory inventory LEFT JOIN optimization_results results
                ON results.job_id=inventory.job_id AND results.original_path=inventory.remote_path
                WHERE inventory.job_id=? AND {where} ORDER BY inventory.id LIMIT ?""",
                (job_id, *parameters, limit)).fetchall()
        return tuple(BrowserImage(*row) for row in rows)

    @staticmethod
    def _record(row: sqlite3.Row) -> ImageRecord:
        return ImageRecord(
            **{key: row[key] for key in InventoryRepository.COLUMNS if key not in {
                "has_alpha", "has_semitransparency", "is_animated", "has_exif", "has_icc_profile",
                "suspicious", "unnecessary_metadata", "signature_valid", "asset_class",
            }},
            has_alpha=bool(row["has_alpha"]), has_semitransparency=bool(row["has_semitransparency"]),
            is_animated=bool(row["is_animated"]), has_exif=bool(row["has_exif"]),
            has_icc_profile=bool(row["has_icc_profile"]), suspicious=bool(row["suspicious"]),
            unnecessary_metadata=bool(row["unnecessary_metadata"]), signature_valid=bool(row["signature_valid"]),
            asset_class=AssetClass(row["asset_class"]),
        )
