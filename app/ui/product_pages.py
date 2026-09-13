"""Lightweight operational pages backed by bounded persistent queries."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QPushButton, QTabWidget,
                               QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget)

from app.core.engine import JobEngine
from app.core.jobs import JobStatus
from app.database.safety import SafetyRepository
from app.ui.offline_page import OfflinePage
from app.ui.online_page import OnlinePipelinePage


def _heading(layout, title: str, subtitle: str) -> None:
    label = QLabel(title); label.setObjectName("pageHeading"); layout.addWidget(label)
    detail = QLabel(subtitle); detail.setObjectName("pageSubtitle"); layout.addWidget(detail)


class ScanPage(QWidget):
    """One workflow surface for local and remote scans."""
    def __init__(self, project_data: Path, engine: JobEngine, settings, resources=None) -> None:
        super().__init__(); layout = QVBoxLayout(self); layout.setContentsMargins(28, 24, 28, 24)
        _heading(layout, "Scan", "Choose an offline folder or continue a verified online website workflow.")
        self.tabs = QTabWidget(); self.offline = OfflinePage(project_data, engine, settings, resources); self.online = OnlinePipelinePage()
        self.tabs.addTab(self.offline, "Local folder"); self.tabs.addTab(self.online, "Website")
        layout.addWidget(self.tabs)


class JobsPage(QWidget):
    rollback_requested = Signal(str)
    def __init__(self, engine: JobEngine, project_data: Path) -> None:
        super().__init__(); self.engine, self.project_data = engine, project_data
        layout = QVBoxLayout(self); layout.setContentsMargins(28, 24, 28, 24)
        _heading(layout, "Jobs", "Persistent history, recovery actions, reports, and rollback availability.")
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(("Job", "Site / target", "Date", "Status", "Files", "Savings", "Duration", "App version"))
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        for text, callback in (("View", self._view), ("Resume", self._resume), ("Cancel", self._cancel),
                               ("Rollback", self._rollback), ("Export report", self._export)):
            button = QPushButton(text); button.clicked.connect(callback); actions.addWidget(button)
        actions.addStretch(); layout.addLayout(actions); self.refresh()

    def refresh(self) -> None:
        jobs = self.engine.repository.list_jobs(limit=200); self.table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            values = (job.id[:8], job.target, job.created_at[:19], job.status.value,
                f"{job.files_completed:,}/{job.files_total:,}", _bytes(job.saved_bytes),
                _duration(job.duration_seconds), job.application_version)
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value)); cell.setData(256, job.id); self.table.setItem(row, column, cell)

    def _selected(self):
        row = self.table.currentRow(); return self.engine.repository.get(self.table.item(row, 0).data(256)) if row >= 0 else None
    def _view(self):
        job = self._selected()
        if job: self.table.setToolTip(json.dumps(job.to_dict(), indent=2))
    def _resume(self):
        job = self._selected()
        if job and job.status in {JobStatus.CREATED, JobStatus.PAUSED, JobStatus.RECOVERABLE}: self.engine.resume(job.id); self.refresh()
    def _cancel(self):
        job = self._selected()
        if job and job.status not in {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED}: self.engine.cancel(job.id); self.refresh()
    def _rollback(self):
        job = self._selected()
        if job: self.rollback_requested.emit(job.id)
    def _export(self):
        job = self._selected()
        if not job: return
        default = self.project_data / "manifests" / f"job-{job.id}.json"
        path, _ = QFileDialog.getSaveFileName(self, "Export job report", str(default), "JSON (*.json)")
        if path: Path(path).write_text(json.dumps(job.to_dict(), indent=2) + "\n", encoding="utf-8")


class BackupsPage(QWidget):
    def __init__(self, engine: JobEngine) -> None:
        super().__init__(); self.repository = SafetyRepository(engine.repository)
        layout = QVBoxLayout(self); layout.setContentsMargins(28, 24, 28, 24)
        _heading(layout, "Backups", "Verified job bundles and retention status.")
        self.table = QTableWidget(0, 4); self.table.setHorizontalHeaderLabels(("Job", "Bundle", "Verified", "Created")); layout.addWidget(self.table, 1)
        with engine.repository.connection() as connection:
            rows = connection.execute("SELECT * FROM backup_bundles ORDER BY created_at DESC LIMIT 200").fetchall()
        self.table.setRowCount(len(rows))
        for row, record in enumerate(rows):
            for column, value in enumerate((record["job_id"][:8], record["bundle_path"], "Yes" if record["verified"] else "No", record["created_at"][:19])):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))


class RecoveryPage(QWidget):
    def __init__(self, engine: JobEngine) -> None:
        super().__init__(); self.engine = engine; layout = QVBoxLayout(self); layout.setContentsMargins(28, 24, 28, 24)
        _heading(layout, "Recovery", "Interrupted jobs and their last durable safe boundaries.")
        self.table = QTableWidget(0, 4); self.table.setHorizontalHeaderLabels(("Job", "Target", "State", "Last checkpoint")); layout.addWidget(self.table, 1)
        refresh = QPushButton("Refresh recovery state"); refresh.clicked.connect(self.refresh); layout.addWidget(refresh); self.refresh()
    def refresh(self):
        jobs = self.engine.repository.list_jobs(unfinished_only=True, limit=200); self.table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            checkpoint = self.engine.repository.last_safe_checkpoint(job.id)
            for column, value in enumerate((job.id[:8], job.target, job.status.value, checkpoint["operation"] if checkpoint else "None")):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))


class LiveLogsPage(QWidget):
    LEVELS = ("ALL", "INFO", "OK", "WARNING", "ERROR", "CRITICAL", "SKIPPED", "RESUMED")
    def __init__(self, engine: JobEngine) -> None:
        super().__init__(); self.engine, self.last_id = engine, 0
        layout = QVBoxLayout(self); layout.setContentsMargins(28, 24, 28, 24)
        _heading(layout, "Logs", "Live, bounded operational event stream.")
        self.output = QTextEdit(); self.output.setReadOnly(True); self.output.setObjectName("logViewer"); layout.addWidget(self.output, 1)
        self.timer = QTimer(self); self.timer.timeout.connect(self.poll); self.timer.start(1000); self.poll()
    def poll(self):
        with self.engine.repository.connection() as connection:
            rows = connection.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT 200", (self.last_id,)).fetchall()
        for row in rows:
            self.last_id = row["id"]; level = _event_level(row["event_type"], row["message"])
            self.output.append(f"[{row['created_at'][11:19]}] {level:<8} {row['message']}")
        if self.output.document().blockCount() > 2000:
            self.output.setPlainText("\n".join(self.output.toPlainText().splitlines()[-1000:]))


def _event_level(event: str, message: str) -> str:
    if event == "ERROR": return "ERROR"
    if "CANCEL" in event or "SKIP" in event: return "SKIPPED"
    if "RESUM" in event: return "RESUMED"
    if event in {"STATE_CHANGED", "ITEM_STATE_CHANGED"} and "FAILED" not in message: return "OK"
    if "FAIL" in event or "FAIL" in message.upper(): return "WARNING"
    return "INFO"


def _bytes(value):
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024 or unit == "GB": return f"{amount:.1f} {unit}"
        amount /= 1024
def _duration(seconds):
    value = int(seconds); return f"{value//3600:02d}:{value//60%60:02d}:{value%60:02d}"
