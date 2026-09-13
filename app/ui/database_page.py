"""Review gate for WordPress database reference dry runs."""

from __future__ import annotations

from itertools import islice

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from app.database.wordpress import WordPressReferenceUpdater


class DatabaseChangesPage(QWidget):
    approved = Signal()

    def __init__(self) -> None:
        super().__init__(); self.updater: WordPressReferenceUpdater | None = None
        layout = QVBoxLayout(self); layout.setContentsMargins(28, 24, 28, 24)
        title = QLabel("Database Reference Review"); title.setObjectName("pageHeading"); layout.addWidget(title)
        self.summary = QLabel("Run the online pipeline through Database Preparation to generate a dry run.")
        self.summary.setWordWrap(True); layout.addWidget(self.summary)
        self.records = QTableWidget(0, 7)
        self.records.setHorizontalHeaderLabels(("Status", "Table", "Record ID", "Column", "Type", "Old reference", "New reference"))
        layout.addWidget(self.records, 1)
        self.approve_button = QPushButton("Approve reviewed changes"); self.approve_button.setObjectName("primaryButton")
        self.approve_button.setEnabled(False); self.approve_button.clicked.connect(self._approve); layout.addWidget(self.approve_button)

    def configure(self, updater: WordPressReferenceUpdater) -> None:
        self.updater = updater; self.refresh()

    def refresh(self) -> None:
        self.records.setRowCount(0)
        if not self.updater: return
        summary = self.updater.repository.summary(self.updater.job_id)
        for record in islice(self.updater.dry_run_records(), 500):
            row = self.records.rowCount(); self.records.insertRow(row)
            values = (record.status, record.table_name, record.record_id, record.column_name,
                      record.change_type, record.old_value, record.new_value or record.review_reason or "No automatic change")
            for column, value in enumerate(values): self.records.setItem(row, column, QTableWidgetItem(str(value)))
        review = summary.get("REVIEW", 0); proposed = summary.get("PROPOSED", 0)
        self.summary.setText(f"{proposed} proposed change(s); {review} record(s) require manual review. "
                             "Showing at most 500 rows; no SQL mutation has occurred.")
        self.approve_button.setEnabled(proposed > 0 and review == 0)

    def _approve(self) -> None:
        if not self.updater: return
        answer = QMessageBox.question(self, "Approve database update",
            "Apply only the displayed changes? A verified full backup will remain in ProjectData.")
        if answer != QMessageBox.Yes: return
        try: self.updater.approve()
        except Exception as exc:
            QMessageBox.critical(self, "Approval blocked", str(exc)); return
        self.approve_button.setEnabled(False); self.summary.setText("Approved. Resume the Online Pipeline to apply and verify these changes.")
        self.approved.emit()
