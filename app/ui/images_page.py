"""Paged, filterable view over the persistent image inventory."""

from __future__ import annotations

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
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            ["Thumbnail", "Original", "Selected", "Format", "Original size", "Final size", "Saving", "Decision", "Confidence", "Status"]
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
        records = self.inventory.browser_page(str(job_id), filter_name=str(self.filter.currentData()), limit=self.PAGE_SIZE + 1)
        visible = records[:self.PAGE_SIZE]
        self.table.setRowCount(len(visible))
        for row, record in enumerate(visible):
            fields = ("▧", record.original_path, record.selected_path or "Original retained", record.format,
                str(record.original_bytes), str(record.final_bytes),
                f"{record.savings_bytes / record.original_bytes * 100:.1f}%" if record.original_bytes else "0%",
                record.decision, record.confidence, record.status)
            for column, value in enumerate(fields):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.notice.setText(
            f"Showing first {self.PAGE_SIZE} matching records; refine the filter to see more."
            if len(records) > self.PAGE_SIZE else f"Showing {len(visible)} matching records."
        )
