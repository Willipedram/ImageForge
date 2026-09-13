"""Responsive local-folder dry-run, preview, apply, recovery, and report UI."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.config.settings import AppSettings
from app.core.engine import JobEngine
from app.database.decisions import DecisionManifestRepository
from app.database.offline import OfflineRepository
from app.database.optimization import OptimizationRepository
from app.image.optimization_models import OptimizationConfig
from app.image.optimizer import OfflineOptimizer
from app.offline.workflow import OfflineWorkflow


class OfflineWorker(QObject):
    progress = Signal(int, int, str, float, float, float)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, workflow: OfflineWorkflow, action: str, job_id: str | None = None,
                 folder: Path | None = None) -> None:
        super().__init__()
        self.workflow, self.action, self.job_id, self.folder = workflow, action, job_id, folder
        self.started = time.monotonic()

    def _progress(self, completed: int, total: int, current: str) -> None:
        elapsed = max(.001, time.monotonic() - self.started)
        rate = completed / elapsed
        eta = (total - completed) / rate if rate else 0.0
        self.progress.emit(completed, total, current, elapsed, eta, rate)

    @Slot()
    def run(self) -> None:
        try:
            if self.action == "new":
                assert self.folder is not None
                self.job_id = self.workflow.create(self.folder)
                self.workflow.scan(self.job_id)
                report = self.workflow.dry_run(self.job_id, self._progress)
            elif self.action == "apply":
                report = self.workflow.apply(str(self.job_id), confirmed=True, progress=self._progress)
            else:
                report = self.workflow.resume(str(self.job_id), self._progress)
            self.finished.emit((self.job_id, report))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class OfflinePage(QWidget):
    def __init__(self, project_data: Path, engine: JobEngine, settings: AppSettings) -> None:
        super().__init__()
        self.engine = engine
        self.offline_repository = OfflineRepository(engine.repository)
        optimization_repository = OptimizationRepository(engine.repository)
        config = OptimizationConfig(
            max_pixels=settings.optimization_max_pixels,
            max_file_bytes=settings.optimization_max_file_mb * 1024 * 1024,
            timeout_seconds=settings.optimization_timeout_seconds,
            max_workers=settings.max_workers,
            decision_profile=settings.default_optimization_profile.upper(),
            minimum_savings_ratio=settings.minimum_savings_percent / 100,
            preserve_candidate_previews=True,
        )
        self.workflow = OfflineWorkflow(
            project_data, engine,
            OfflineOptimizer(project_data, optimization_repository, config),
            self.offline_repository,
        )
        self.manifests = DecisionManifestRepository(engine.repository)
        self.job_id: str | None = self.offline_repository.latest_unfinished_job()
        self.thread: QThread | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        title = QLabel("Offline Optimization")
        title.setObjectName("pageHeading")
        root.addWidget(title)
        self.folder_label = QLabel("Select a local folder. Scanning and dry runs never modify originals.")
        root.addWidget(self.folder_label)
        actions = QHBoxLayout()
        for text, callback in (
            ("Select folder & dry run", self.select_folder), ("Apply…", self.apply),
            ("Pause", self.pause), ("Resume", self.resume), ("Cancel", self.cancel),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            actions.addWidget(button)
        actions.addStretch()
        root.addLayout(actions)
        self.stage = QLabel("Ready")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.telemetry = QLabel("Elapsed 00:00 · ETA — · 0.0 files/s")
        root.addWidget(self.stage)
        root.addWidget(self.progress)
        root.addWidget(self.telemetry)
        previews = QHBoxLayout()
        self.original_preview = QLabel("Original preview")
        self.webp_preview = QLabel("WebP preview")
        self.avif_preview = QLabel("AVIF preview")
        self.selected_preview = QLabel("Selected preview")
        for label in (self.original_preview, self.webp_preview, self.avif_preview, self.selected_preview):
            label.setAlignment(Qt.AlignCenter)
            label.setMinimumHeight(150)
            label.setStyleSheet("background:white;border:1px solid #dce3ed;")
            previews.addWidget(label)
        root.addLayout(previews)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "Original", "Dimensions", "Original bytes", "WebP bytes", "AVIF bytes",
            "Selected", "Savings", "Quality", "Decision reason",
        ])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._preview_selected)
        root.addWidget(self.table, 1)
        self.report = QLabel("No report yet.")
        root.addWidget(self.report)
        if self.job_id:
            self.stage.setText("Interrupted offline job found — Resume, Apply, or inspect its preview.")
            self._load_preview()

    @Slot()
    def select_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Select local image folder")
        if selected:
            self.folder_label.setText(selected)
            self._start("new", folder=Path(selected))

    @Slot()
    def apply(self) -> None:
        if not self.job_id:
            return
        answer = QMessageBox.question(
            self, "Apply proposed changes",
            "Backups will be created before atomic local replacement. Apply validated changes?",
        )
        if answer == QMessageBox.Yes:
            self._start("apply")

    @Slot()
    def pause(self) -> None:
        if self.job_id:
            try: self.workflow.pause(self.job_id)
            except Exception as exc: self.stage.setText(str(exc))

    @Slot()
    def resume(self) -> None:
        if self.job_id:
            self._start("resume")

    @Slot()
    def cancel(self) -> None:
        if self.job_id:
            try: self.workflow.cancel(self.job_id)
            except Exception as exc: self.stage.setText(str(exc))

    def _start(self, action: str, folder: Path | None = None) -> None:
        if self.thread and self.thread.isRunning():
            return
        self.stage.setText("Working…")
        thread = QThread(self)
        worker = OfflineWorker(self.workflow, action, self.job_id, folder)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._progress)
        worker.finished.connect(self._finished)
        worker.failed.connect(self._failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._thread_finished)
        self.thread, self.worker = thread, worker
        thread.start()

    @Slot(int, int, str, float, float, float)
    def _progress(self, completed: int, total: int, current: str,
                  elapsed: float, eta: float, rate: float) -> None:
        self.progress.setValue(round(completed / total * 100) if total else 0)
        self.stage.setText(f"Processing: {current} ({completed}/{total})")
        self.telemetry.setText(f"Elapsed {elapsed:.1f}s · ETA {eta:.1f}s · {rate:.2f} files/s")

    @Slot(object)
    def _finished(self, payload) -> None:
        self.job_id, report = payload
        self.stage.setText("Preview ready" if self.engine.repository.get(self.job_id).status.value == "PREVIEW" else "Complete")
        self.progress.setValue(100)
        self.report.setText(
            f"Images {report.total_images} · optimized {report.optimized} · skipped {report.skipped} · "
            f"failed {report.failed} · {report.savings_bytes:,} bytes saved ({report.reduction_percent:.1f}%) · "
            f"formats {report.formats_selected} · {report.duration_seconds:.1f}s"
        )
        self._load_preview()

    @Slot(str)
    def _failed(self, message: str) -> None:
        self.stage.setText(message)

    @Slot()
    def _thread_finished(self) -> None:
        self.thread = None

    def _load_preview(self) -> None:
        self.table.setRowCount(0)
        if not self.job_id:
            return
        for item in self.workflow.previews(self.job_id):
            manifest = self.manifests.get(self.job_id, item.relative_path) or {}
            sizes = {entry["format"]: entry["bytes"] for entry in manifest.get("candidate_assessments", [])}
            row = self.table.rowCount()
            self.table.insertRow(row)
            savings = item.original_bytes - (item.candidate_bytes or item.original_bytes)
            values = (
                item.relative_path, f"{item.width or '—'}×{item.height or '—'}", str(item.original_bytes),
                str(sizes.get("WEBP", "—")), str(sizes.get("AVIF", "—")),
                item.candidate_format or "ORIGINAL", str(max(0, savings)),
                manifest.get("confidence", "—"), item.decision_reason or item.error or "—",
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                candidates = {
                    entry["format"]: entry["path"]
                    for entry in manifest.get("candidate_assessments", [])
                }
                cell.setData(Qt.UserRole, (
                    item.source_path if Path(item.source_path).exists() else item.backup_path,
                    candidates.get("WEBP"), candidates.get("AVIF"), item.candidate_path,
                ))
                self.table.setItem(row, column, cell)

    @Slot()
    def _preview_selected(self) -> None:
        selected = self.table.selectedItems()
        if not selected:
            return
        source, webp, avif, candidate = selected[0].data(Qt.UserRole)
        self._set_pixmap(self.original_preview, source, "Original")
        self._set_pixmap(self.webp_preview, webp, "WebP")
        self._set_pixmap(self.avif_preview, avif, "AVIF")
        self._set_pixmap(self.selected_preview, candidate, "Selected")

    @staticmethod
    def _set_pixmap(label: QLabel, path: str | None, fallback: str) -> None:
        pixmap = QPixmap(path or "")
        if pixmap.isNull():
            label.setText(fallback + " preview unavailable")
        else:
            label.setPixmap(pixmap.scaled(360, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation))
