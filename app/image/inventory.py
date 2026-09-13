"""Streaming remote analysis that persists bounded batches immediately."""

from __future__ import annotations

import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from app.database.inventory import InventoryRepository
from app.image.intelligence import ImageAnalyzer
from app.image.wordpress import AttachmentMetadata, DERIVATIVE_PATTERN
from app.server.base import RemoteServer
from app.server.discovery import RemoteImage


class InventoryBuilder:
    def __init__(self, server: RemoteServer, repository: InventoryRepository,
                 analyzer: ImageAnalyzer | None = None, batch_size: int = 50) -> None:
        self.server = server
        self.repository = repository
        self.analyzer = analyzer or ImageAnalyzer()
        self.batch_size = batch_size

    def build(self, job_id: str, images: Iterable[RemoteImage]) -> int:
        batch = []
        total = 0
        for image in images:
            suffix = PurePosixPath(image.path).suffix
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
                local_path = Path(temporary.name)
            try:
                self.server.download(image.path, local_path)
                record = self.analyzer.analyze(local_path, job_id=job_id, remote_path=image.path)
                # Preserve authoritative remote size even if transfer wrappers differ.
                record.size = image.size if image.size is not None else record.size
                batch.append(record)
            finally:
                local_path.unlink(missing_ok=True)
            if len(batch) >= self.batch_size:
                total += self.repository.save_batch(batch)
                batch.clear()
        total += self.repository.save_batch(batch)
        self.map_filename_derivatives(job_id)
        return total

    def map_filename_derivatives(self, job_id: str) -> None:
        for record in self.repository.iter_records(job_id):
            path = PurePosixPath(record.remote_path)
            match = DERIVATIVE_PATTERN.match(path.stem)
            if not match:
                continue
            parent = str(path.with_name(match.group("base") + path.suffix))
            if self.repository.get_by_path(job_id, parent):
                self.repository.set_relationship(
                    job_id, record.remote_path, parent,
                    f"{match.group('width')}x{match.group('height')}",
                )

    def apply_wordpress_metadata(self, job_id: str, attachments: Iterable[AttachmentMetadata]) -> None:
        for attachment in attachments:
            if self.repository.get_by_path(job_id, attachment.original_path):
                self.repository.set_attachment(job_id, attachment.original_path, attachment.attachment_id)
            for derivative in attachment.derivatives:
                if self.repository.get_by_path(job_id, derivative):
                    self.repository.set_relationship(
                        job_id, derivative, attachment.original_path,
                        "WORDPRESS_METADATA", attachment.attachment_id,
                    )
