from __future__ import annotations

import sqlite3
import zlib
from pathlib import Path

import pytest

from app.core.engine import JobEngine
from app.database.inventory import InventoryRepository
from app.database.jobs import DATABASE_SCHEMA_VERSION, JobRepository
from app.image.intelligence import ImageAnalyzer
from app.image.inventory import InventoryBuilder
from app.image.models import AssetClass, ImageRecord
from app.image.wordpress import AttachmentMetadata, discover_wordpress_tables, map_derivatives
from app.server.discovery import RemoteImage


def analyze(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return ImageAnalyzer().analyze(path, job_id="job", remote_path=f"/uploads/{name}")


def test_jpeg_signature_dimensions_and_mismatched_extension(tmp_path):
    jpeg = b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x64\x00\xc8\x03" + b"\x00" * 20
    record = analyze(tmp_path, "photo.png", jpeg)
    assert record.detected_format == "JPEG"
    assert (record.width, record.height) == (200, 100)
    assert record.mime_type == "image/jpeg" and record.signature_valid


def test_png_alpha_metadata(tmp_path):
    def chunk(kind, payload):
        return len(payload).to_bytes(4, "big") + kind + payload + b"\x00\x00\x00\x00"
    ihdr = (2).to_bytes(4, "big") + (1).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    pixels = b"\x00" + b"\xff\x00\x00\x00" + b"\x00\xff\x00\x80"
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")
    record = analyze(tmp_path, "透明.png", png)
    assert record.has_alpha and record.color_mode == "RGBA"
    assert record.aspect_ratio == 2.0
    assert record.transparency_ratio == 1.0 and record.has_semitransparency


def test_animated_gif_is_preserved_as_animation(tmp_path):
    gif = b"GIF89a\x10\x00\x10\x00" + b"\x00" * 10 + b"\x2c" + b"\x00" * 10 + b"\x2c"
    record = analyze(tmp_path, "motion.gif", gif)
    assert record.is_animated and record.frame_count == 2
    assert record.asset_class is AssetClass.ANIMATION


def test_webp_and_animated_avif_signatures(tmp_path):
    webp = b"RIFF" + b"\x20\x00\x00\x00WEBPVP8X" + b"\x0a\x00\x00\x00" + bytes([0x10, 0, 0, 0]) + (99).to_bytes(3, "little") + (49).to_bytes(3, "little")
    webp_record = analyze(tmp_path, "graphic.webp", webp)
    assert webp_record.detected_format == "WEBP" and webp_record.has_alpha
    assert (webp_record.width, webp_record.height) == (100, 50)
    avif = b"\x00\x00\x00\x18ftypavis" + b"\x00" * 20
    avif_record = analyze(tmp_path, "clip.avif", avif)
    assert avif_record.detected_format == "AVIF" and avif_record.is_animated
    assert avif_record.skip_reason.startswith("Animated AVIF")


def test_svg_analysis_flags_metadata_and_active_content(tmp_path):
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="120" height="40"><metadata>editor</metadata><script>alert(1)</script><rect opacity=".5"/></svg>'''
    record = analyze(tmp_path, "logo.svg", svg)
    assert record.detected_format == "SVG" and (record.width, record.height) == (120, 40)
    assert record.suspicious and record.unnecessary_metadata and record.has_alpha
    assert record.asset_class is AssetClass.VECTOR


def test_invalid_signature_is_skipped(tmp_path):
    record = analyze(tmp_path, "pretend.jpg", b"not an image")
    assert not record.signature_valid and record.suspicious and record.skip_reason


def image(job, path):
    return ImageRecord(job, path, Path(path).name, Path(path).suffix, "JPEG", "image/jpeg", 10)


def test_derivative_mapping_uses_filename_and_attachment_metadata():
    original = image("job", "/uploads/product.jpg")
    thumb = image("job", "/uploads/product-300x300.jpg")
    custom = image("job", "/uploads/product-shop.jpg")
    records = [original, thumb, custom]
    map_derivatives(records, [AttachmentMetadata(42, original.remote_path, (custom.remote_path,))])
    assert thumb.parent_path == original.remote_path and thumb.derivative_kind == "300x300"
    assert custom.attachment_id == 42 and custom.derivative_kind == "WORDPRESS_METADATA"
    assert thumb.asset_class is AssetClass.THUMBNAIL


def test_wordpress_table_prefix_is_discovered():
    result = discover_wordpress_tables({
        "client7_posts", "client7_postmeta", "client7_options", "client7_termmeta",
        "client7_terms", "unrelated_posts",
    })
    assert result.prefix == "client7_"
    assert result.posts == "client7_posts" and result.termmeta == "client7_termmeta"
    assert discover_wordpress_tables({"wp_posts", "wp_options"}) is None


@pytest.fixture
def inventory(tmp_path):
    jobs = JobRepository(tmp_path / "state.db")
    jobs.initialize()
    job = JobEngine(jobs).create_job("example.com")
    return InventoryRepository(jobs), job


def test_inventory_persists_unicode_relationships_and_summary(inventory):
    repository, job = inventory
    records = [
        image(job.id, "/uploads/café.jpg"),
        ImageRecord(job.id, "/uploads/logo.png", "logo.png", ".png", "PNG", "image/png", 20,
                    has_alpha=True, transparency_ratio=.5, asset_class=AssetClass.LOGO),
    ]
    repository.save_batch(records)
    repository.set_relationship(job.id, records[1].remote_path, records[0].remote_path, "150x150")
    summary = repository.summary(job.id)
    assert summary.total_images == 2 and summary.total_bytes == 30
    assert summary.transparent == 1 and summary.derivatives == 1
    assert repository.get_by_path(job.id, "/uploads/café.jpg").filename == "café.jpg"
    assert len(list(repository.iter_records(job.id, filter_name="transparent"))) == 1


def test_database_migrates_version_one_to_inventory_schema(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE legacy_marker(value TEXT)")
        connection.execute("PRAGMA user_version=1")
    JobRepository(path).initialize()
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert "image_inventory" in tables
    assert version == DATABASE_SCHEMA_VERSION == 9


def test_inventory_builder_streams_files_and_maps_derivatives(inventory):
    repository, job = inventory
    jpeg = b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x0a\x00\x14\x03" + b"\x00" * 20

    class DownloadServer:
        def __init__(self): self.downloads = 0
        def download(self, remote_path, destination):
            self.downloads += 1
            destination.write_bytes(jpeg)

    server = DownloadServer()
    images = (
        RemoteImage("/uploads/产品.jpg", ".jpg", len(jpeg), None, "image/jpeg"),
        RemoteImage("/uploads/产品-150x150.jpg", ".jpg", len(jpeg), None, "image/jpeg"),
    )
    count = InventoryBuilder(server, repository, batch_size=1).build(job.id, iter(images))
    assert count == 2 and server.downloads == 2
    derivative = repository.get_by_path(job.id, "/uploads/产品-150x150.jpg")
    assert derivative.parent_path == "/uploads/产品.jpg"
