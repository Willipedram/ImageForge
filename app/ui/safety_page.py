"""Final audit and job-ID rollback UI."""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton, QTextEdit, QVBoxLayout, QWidget

from app.safety.coordinator import FinalSafetyCoordinator


class RollbackWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, coordinator, job_id):
        super().__init__(); self.coordinator, self.job_id = coordinator, job_id

    @Slot()
    def run(self):
        try: self.finished.emit(self.coordinator.rollback(self.job_id))
        except Exception as exc: self.failed.emit(f"{type(exc).__name__}: {exc}")


class SafetyPage(QWidget):
    def __init__(self) -> None:
        super().__init__(); self.coordinator: FinalSafetyCoordinator | None = None; self.job_id = None; self._thread = None
        layout = QVBoxLayout(self); layout.setContentsMargins(28, 24, 28, 24)
        title = QLabel("Final Safety & Rollback"); title.setObjectName("pageHeading"); layout.addWidget(title)
        layout.addWidget(QLabel("Original cleanup is authorized only when every audit gate passes."))
        self.report = QTextEdit(); self.report.setReadOnly(True)
        self.report.setPlaceholderText("Select a completed online job to inspect its final audit.")
        layout.addWidget(self.report, 1)
        self.rollback_button = QPushButton("Rollback by Job ID"); self.rollback_button.setEnabled(False)
        self.rollback_button.clicked.connect(self._rollback); layout.addWidget(self.rollback_button)

    def configure(self, coordinator: FinalSafetyCoordinator, job_id: str) -> None:
        self.coordinator, self.job_id = coordinator, job_id; self.rollback_button.setEnabled(True); self.refresh()

    def refresh(self) -> None:
        if not self.coordinator or not self.job_id: return
        report = self.coordinator.report(self.job_id)
        self.report.setPlainText("\n".join((f"Optimized: {report.optimized}", f"Skipped: {report.skipped}",
            f"Failed: {report.failed}", f"Originals deleted: {report.originals_deleted}",
            f"Originals retained: {report.originals_retained}", f"Database records updated: {report.database_records_updated}",
            f"Total savings: {report.total_savings} bytes", f"Duration: {report.duration_seconds:.1f} s",
            f"Verification errors: {len(report.verification_errors)}", f"Warnings: {len(report.warnings)}")))

    def _rollback(self) -> None:
        if not self.coordinator or not self.job_id or self._thread and self._thread.isRunning(): return
        if QMessageBox.warning(self, "Confirm rollback", "Restore originals and the verified database backup for this job?",
                               QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes: return
        thread = QThread(self); worker = RollbackWorker(self.coordinator, self.job_id); worker.moveToThread(thread)
        thread.started.connect(worker.run); worker.finished.connect(self._done); worker.failed.connect(self._failed)
        worker.finished.connect(thread.quit); worker.failed.connect(thread.quit); thread.finished.connect(worker.deleteLater)
        self._thread, self._worker = thread, worker; self.rollback_button.setEnabled(False); thread.start()

    @Slot(object)
    def _done(self, report) -> None:
        self.report.append(f"\nRollback completed: {report.originals_restored} originals restored; database restored.")
        self.rollback_button.setEnabled(True)

    @Slot(str)
    def _failed(self, error) -> None:
        self.report.append("\nRollback failed safely: " + error); self.rollback_button.setEnabled(True)
