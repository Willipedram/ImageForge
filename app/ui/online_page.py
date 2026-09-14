"""Threaded online pipeline monitor; protocol and DB adapters are injected at runtime."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar,
                               QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from app.core.jobs import JobStatus
from app.database.optimization import OptimizationRepository
from app.image.optimizer import OfflineOptimizer
from app.online.workflow import OnlineWorkflow
from app.server import ConnectionConfig, RuntimeCredentials, create_server
from app.utils.checkpoints import log_keypoint
from app.utils.logging import read_live_logs

logger = logging.getLogger(__name__)


class OnlineWorker(QObject):
    progressed = Signal(str, int, int, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, workflow: OnlineWorkflow, job_id: str, action: str = "scan") -> None:
        super().__init__(); self.workflow, self.job_id, self.action = workflow, job_id, action

    @Slot()
    def run(self) -> None:
        try:
            operation = (self.workflow.scan_only if self.action == "scan"
                         else self.workflow.prepare_preview)
            self.finished.emit((self.action, operation(self.job_id, self.progressed.emit)))
        except Exception as exc:
            log_keypoint(logger, "website_scan", "stopped", job_id=self.job_id, error=exc)
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            if self.action == "preview":
                self.workflow.server.forget_credentials()


class OnlinePipelinePage(QWidget):
    """Displays byte/file telemetry while all network work runs on a QThread."""

    def __init__(self, project_data: Path | None = None, engine=None, resources=None) -> None:
        super().__init__(); self.workflow = None; self.job_id = None; self._thread = None; self.started = 0.0
        self.project_data, self.engine, self.resources = project_data, engine, resources
        self._connection: tuple[ConnectionConfig, RuntimeCredentials, object] | None = None
        root = QVBoxLayout(self); root.setContentsMargins(28, 24, 28, 24)
        title = QLabel("Online Website Pipeline"); title.setObjectName("pageHeading"); root.addWidget(title)
        self.guide = QLabel("Step 1: verify server  →  Step 2: scan website  →  Step 3: review images")
        self.guide.setObjectName("pageSubtitle"); root.addWidget(self.guide)
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
        self.start_button = QPushButton("Scan website"); self.start_button.setObjectName("primaryButton")
        self.pause_button, self.cancel_button = QPushButton("Pause"), QPushButton("Cancel")
        for button in (self.start_button, self.pause_button, self.cancel_button): actions.addWidget(button)
        actions.addStretch(); root.addLayout(actions)
        self.start_button.clicked.connect(self._start); self.pause_button.clicked.connect(self._pause); self.cancel_button.clicked.connect(self._cancel)
        self.items = QTableWidget(0, 5); self.items.setHorizontalHeaderLabels(("Remote image", "Decision", "Upload", "Verify", "Database"))
        root.addWidget(self.items, 1)
        root.addWidget(QLabel("Live scan activity"))
        self.live_log = QPlainTextEdit(); self.live_log.setReadOnly(True)
        self.live_log.setMaximumBlockCount(400); self.live_log.setMaximumHeight(150)
        self.live_log.setPlaceholderText("Folder-by-folder scan activity will appear here.")
        root.addWidget(self.live_log)
        self._last_log_id = 0
        self._log_timer = QTimer(self); self._log_timer.timeout.connect(self._poll_logs)
        self._log_timer.start(300)

    def configure(self, workflow: OnlineWorkflow, job_id: str) -> None:
        self.workflow, self.job_id = workflow, job_id; self.stage.setText("Ready")

    @Slot(object, object, object)
    def configure_connection(self, config: ConnectionConfig, credentials: RuntimeCredentials, discovery) -> None:
        if self.job_id and self.engine:
            previous = self.engine.repository.get(self.job_id)
            if previous and previous.status is JobStatus.PAUSED:
                self.engine.cancel(self.job_id)
        root = discovery.site_root or config.remote_root
        self._connection = (replace(config, remote_root=root), credentials, discovery)
        self.workflow = self.job_id = None
        self.stage.setText(f"Ready to scan {discovery.uploads}")
        self.guide.setText("✓ Step 1 complete  →  Step 2: click Scan website  →  Step 3: review results")
        self.current.setText("Connection verified on Servers page")
        self.start_button.setText("Scan website")
        self.start_button.setProperty("nextAction", None)
        self.start_button.setEnabled(True)

    def _start(self) -> None:
        if self._thread and self._thread.isRunning():
            return
        if not self.workflow or not self.job_id:
            if not self._connection or not self.project_data or not self.engine:
                self.stage.setText("Not configured")
                self.current.setText("First open Servers and run Test & discover successfully.")
                return
            config, credentials, discovery = self._connection
            server = create_server(config, credentials)
            self.workflow = OnlineWorkflow(
                self.project_data, self.engine, server,
                OfflineOptimizer(
                    self.project_data,
                    OptimizationRepository(self.engine.repository),
                    resources=self.resources,
                ),
                None, resources=self.resources,
                public_base_url=(f"https://{config.website_domain}"
                                 if config.website_domain else None),
            )
            self.job_id = self.workflow.create(config.host, discovery.site_root or config.remote_root)
            self._connection = None
        action = "preview" if self.start_button.property("nextAction") == "preview" else "scan"
        self.started = time.monotonic(); thread = QThread(self); worker = OnlineWorker(self.workflow, self.job_id, action); worker.moveToThread(thread)
        self.guide.setText("✓ Step 1  →  ● Step 2: scanning now  →  Step 3: review results")
        thread.started.connect(worker.run); worker.progressed.connect(self._progressed); worker.finished.connect(self._finished)
        worker.failed.connect(self._failed); worker.finished.connect(thread.quit); worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        self._thread, self._worker = thread, worker; self.start_button.setEnabled(False); thread.start()

    @Slot(str, int, int, str)
    def _progressed(self, stage, done, total, current) -> None:
        elapsed = max(.001, time.monotonic() - self.started); self.stage.setText(stage); self.current.setText(current)
        self.files.setText(f"{done} / {total}"); self.progress.setValue(round(done / total * 100) if total else 0)
        self.elapsed.setText(f"{elapsed:.1f} s"); self.eta.setText(f"{max(0, total-done)/(done/elapsed):.1f} s" if done else "—")

    @Slot(object)
    def _finished(self, result) -> None:
        action, report = result
        self.bytes.setText(f"{report.bytes_downloaded} B / {report.bytes_uploaded} B")
        self.retries.setText(str(report.retries)); self.errors.setText(str(report.failed)); self._refresh()
        if action == "scan":
            self.stage.setText("Inventory ready — continue to create a local optimization preview")
            self.start_button.setText("Download & prepare preview")
            self.start_button.setProperty("nextAction", "preview")
            self.start_button.setEnabled(True)
            self.guide.setText("✓ Connection  →  ✓ Inventory  →  Step 3: prepare a safe local preview")
        else:
            self.stage.setText("Optimization preview ready — no website files were changed")
            self.start_button.setText("Preview complete")
            self.start_button.setEnabled(False)
            self.guide.setText("✓ Connection  →  ✓ Inventory  →  ✓ Local preview ready for review")

    @Slot(str)
    def _failed(self, message) -> None:
        self.stage.setText("Scan failed — reconnect on Servers to retry")
        self.current.setText(message)
        self.start_button.setText("Reconnect to scan again")
        self._refresh()

    def _poll_logs(self) -> None:
        for record in read_live_logs(self._last_log_id, 200):
            self._last_log_id = record.id
            if record.logger not in {
                "app.online.workflow", "app.ui.online_page", "app.server.ftp"
            }:
                continue
            self.live_log.appendPlainText(
                f"[{record.created_at[11:23]}] {record.level:<7} {record.message}"
            )

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
