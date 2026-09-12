"""Paged, filterable view over the persistent image inventory."""

from __future__ import annotations

from itertools import islice

from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from app.database.inventory import InventoryRepository


class ImagesPage(QWidget):
    PAGE_SIZE = 500

    def __init__(self, inventory: InventoryRepository) -> None:
        super().__init__()
        self.inventory = inventory
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        heading = QLabel("Images")
        heading.setObjectName("pageHeading")
        root.addWidget(heading)
        controls = QHBoxLayout()
        self.jobs = QComboBox()
        for job in inventory.jobs.list_jobs():
            self.jobs.addItem(f"{job.target} · {job.created_at[:19]}", job.id)
        self.filter = QComboBox()
        for label, value in (
            ("All images", "all"), ("Transparent", "transparent"), ("Animated", "animated"),
            ("Derivatives", "derivatives"), ("Suspicious", "suspicious"), ("Skipped", "skipped"),
            ("JPEG", "format:JPEG"), ("PNG", "format:PNG"), ("GIF", "format:GIF"),
            ("WebP", "format:WEBP"), ("AVIF", "format:AVIF"), ("SVG", "format:SVG"),
        ):
            self.filter.addItem(label, value)
        controls.addWidget(QLabel("Job"))
        controls.addWidget(self.jobs, 1)
        controls.addWidget(QLabel("Filter"))
        controls.addWidget(self.filter)
        root.addLayout(controls)
        summary_grid = QGridLayout()
        self.summary_labels = {}
        for index, key in enumerate(("Images", "Bytes", "Formats", "Transparent", "Animated", "Derivatives", "Suspicious", "Skipped")):
            label = QLabel(f"{key}: 0")
            self.summary_labels[key] = label
            summary_grid.addWidget(label, index // 4, index % 4)
        root.addLayout(summary_grid)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ["Filename", "Format", "Dimensions", "Size", "Class", "Alpha", "Animated", "Relationship", "Status"]
        )
        self.table.setSortingEnabled(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self.table, 1)
        self.notice = QLabel("")
        root.addWidget(self.notice)
        self.jobs.currentIndexChanged.connect(self.refresh)
        self.filter.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        job_id = self.jobs.currentData()
        self.table.setRowCount(0)
        if not job_id:
            self.notice.setText("Create and scan a job to populate the inventory.")
            return
        summary = self.inventory.summary(str(job_id))
        values = {
            "Images": summary.total_images, "Bytes": summary.total_bytes,
            "Formats": ", ".join(f"{key} {value}" for key, value in summary.formats.items()) or "—",
            "Transparent": summary.transparent, "Animated": summary.animated,
            "Derivatives": summary.derivatives, "Suspicious": summary.suspicious, "Skipped": summary.skipped,
        }
        for key, value in values.items():
            self.summary_labels[key].setText(f"{key}: {value}")
        records = list(islice(
            self.inventory.iter_records(str(job_id), filter_name=str(self.filter.currentData())),
            self.PAGE_SIZE + 1,
        ))
        visible = records[:self.PAGE_SIZE]
        self.table.setRowCount(len(visible))
        for row, record in enumerate(visible):
            fields = (
                record.filename, record.detected_format or "UNKNOWN",
                f"{record.width}×{record.height}" if record.width and record.height else "—",
                str(record.size), record.asset_class.value, "Yes" if record.has_alpha else "No",
                "Yes" if record.is_animated else "No", record.derivative_kind or "Original",
                record.skip_reason or ("Suspicious" if record.suspicious else "Ready"),
            )
            for column, value in enumerate(fields):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.notice.setText(
            f"Showing first {self.PAGE_SIZE} matching records; refine the filter to see more."
            if len(records) > self.PAGE_SIZE else f"Showing {len(visible)} matching records."
        )
