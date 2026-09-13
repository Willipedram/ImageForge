"""Threaded online pipeline monitor; protocol and DB adapters are injected at runtime."""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from app.online.workflow import OnlineWorkflow


class OnlineWorker(QObject):
    progressed = Signal(str, int, int, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, workflow: OnlineWorkflow, job_id: str) -> None:
        super().__init__(); self.workflow, self.job_id = workflow, job_id

    @Slot()
    def run(self) -> None:
        try: self.finished.emit(self.workflow.run(self.job_id, self.progressed.emit))
        except Exception as exc: self.failed.emit(f"{type(exc).__name__}: {exc}")


class OnlinePipelinePage(QWidget):
    """Displays byte/file telemetry while all network work runs on a QThread."""

    def __init__(self) -> None:
        super().__init__(); self.workflow = None; self.job_id = None; self._thread = None; self.started = 0.0
        root = QVBoxLayout(self); root.setContentsMargins(28, 24, 28, 24)
        title = QLabel("Online Website Pipeline"); title.setObjectName("pageHeading"); root.addWidget(title)
        root.addWidget(QLabel("Verified staging uploads and database reference changes; remote originals are retained."))
        panel = QFrame(); panel.setObjectName("settingsPanel"); grid = QGridLayout(panel)
        self.stage, self.current, self.elapsed, self.eta = QLabel("Not configured"), QLabel("—"), QLabel("0 s"), QLabel("—")
        self.files, self.bytes, self.retries, self.errors = QLabel("0 / 0"), QLabel("0 B / 0 B"), QLabel("0"), QLabel("0")
        for index, (name, value) in enumerate((("Stage", self.stage), ("Current image", self.current),
            ("Files", self.files), ("Bytes download/upload", self.bytes), ("Elapsed", self.elapsed),
            ("ETA", self.eta), ("Retries", self.retries), ("Errors", self.errors))):
            grid.addWidget(QLabel(name), index // 2, (index % 2) * 2); grid.addWidget(value, index // 2, (index % 2) * 2 + 1)
        root.addWidget(panel)
        self.progress = QProgressBar(); root.addWidget(self.progress)
        actions = QHBoxLayout()
        self.start_button = QPushButton("Start / Resume"); self.start_button.setObjectName("primaryButton")
        self.pause_button, self.cancel_button = QPushButton("Pause"), QPushButton("Cancel")
        for button in (self.start_button, self.pause_button, self.cancel_button): actions.addWidget(button)
        actions.addStretch(); root.addLayout(actions)
        self.start_button.clicked.connect(self._start); self.pause_button.clicked.connect(self._pause); self.cancel_button.clicked.connect(self._cancel)
        self.items = QTableWidget(0, 5); self.items.setHorizontalHeaderLabels(("Remote image", "Decision", "Upload", "Verify", "Database"))
        root.addWidget(self.items, 1)

    def configure(self, workflow: OnlineWorkflow, job_id: str) -> None:
        self.workflow, self.job_id = workflow, job_id; self.stage.setText("Ready")

    def _start(self) -> None:
        if not self.workflow or not self.job_id or (self._thread and self._thread.isRunning()): return
        self.started = time.monotonic(); thread = QThread(self); worker = OnlineWorker(self.workflow, self.job_id); worker.moveToThread(thread)
        thread.started.connect(worker.run); worker.progressed.connect(self._progressed); worker.finished.connect(self._finished)
        worker.failed.connect(self._failed); worker.finished.connect(thread.quit); worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater); thread.finished.connect(lambda: self.start_button.setEnabled(True))
        self._thread, self._worker = thread, worker; self.start_button.setEnabled(False); thread.start()

    @Slot(str, int, int, str)
    def _progressed(self, stage, done, total, current) -> None:
        elapsed = max(.001, time.monotonic() - self.started); self.stage.setText(stage); self.current.setText(current)
        self.files.setText(f"{done} / {total}"); self.progress.setValue(round(done / total * 100) if total else 0)
        self.elapsed.setText(f"{elapsed:.1f} s"); self.eta.setText(f"{max(0, total-done)/(done/elapsed):.1f} s" if done else "—")

    @Slot(object)
    def _finished(self, report) -> None:
        self.stage.setText("Completed"); self.bytes.setText(f"{report.bytes_downloaded} B / {report.bytes_uploaded} B")
        self.retries.setText(str(report.retries)); self.errors.setText(str(report.failed)); self._refresh()

    @Slot(str)
    def _failed(self, message) -> None: self.stage.setText("Recoverable error"); self.current.setText(message); self._refresh()

    def _refresh(self) -> None:
        if not self.workflow or not self.job_id: return
        self.items.setRowCount(0)
        for item in self.workflow.manifest(self.job_id):
            row = self.items.rowCount(); self.items.insertRow(row)
            for column, value in enumerate((item.remote_path, item.decision or item.status.value,
                                             item.upload_status, item.verification_status, item.database_status)):
                self.items.setItem(row, column, QTableWidgetItem(str(value)))

    def _pause(self) -> None:
        if self.workflow and self.job_id: self.workflow.pause(self.job_id)

    def _cancel(self) -> None:
        if self.workflow and self.job_id: self.workflow.cancel(self.job_id)
