"""Background dashboard polling; no system or database work runs on Qt's UI thread."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from PySide6.QtCore import QObject, Signal, Slot

from app.core.estimation import TimeEstimator, WorkSample, WorkStage, format_duration
from app.core.jobs import JobStatus
from app.core.monitoring import ResourceMonitor
from app.core.resources import ResourceManager
from app.database.jobs import JobRepository
from app.ui.dashboard import DashboardSnapshot

STATUS_STAGE = {JobStatus.SCANNING: WorkStage.SCAN, JobStatus.DOWNLOADING: WorkStage.DOWNLOAD,
    JobStatus.OPTIMIZING: WorkStage.OPTIMIZE, JobStatus.UPLOADING: WorkStage.UPLOAD,
    JobStatus.DB_UPDATING: WorkStage.DATABASE, JobStatus.VERIFYING: WorkStage.VERIFICATION,
    JobStatus.CLEANUP: WorkStage.CLEANUP}


class DashboardMonitor(QObject):
    snapshot = Signal(object)
    failed = Signal(str)

    def __init__(self, repository: JobRepository, resources: ResourceManager,
                 monitor: ResourceMonitor, interval: float = 1.0) -> None:
        super().__init__(); self.repository, self.resources, self.monitor = repository, resources, monitor
        self.interval = interval; self.running = True; self.estimator = TimeEstimator(self._history())
        self.previous = None

    @Slot()
    def run(self) -> None:
        while self.running:
            started = time.monotonic()
            try: self.snapshot.emit(self._sample(started))
            except Exception as exc: self.failed.emit(f"Monitoring unavailable: {type(exc).__name__}: {exc}")
            remaining = self.interval - (time.monotonic() - started)
            if remaining > 0: time.sleep(remaining)

    @Slot()
    def stop(self) -> None: self.running = False

    def _sample(self, timestamp: float) -> DashboardSnapshot:
        jobs = self.repository.list_jobs(unfinished_only=True); job = jobs[0] if jobs else None
        allocation = self.resources.allocation
        resources = self.monitor.sample(workers_active=self.resources.active_workers, now=timestamp)
        allocation = self.resources.adapt(resources)
        if not job:
            return DashboardSnapshot(cpu=resources.cpu_percent, ram=resources.ram_percent,
                disk=resources.disk_percent, network_down=resources.network_down_bps,
                network_up=resources.network_up_bps, workers=f"0 active · {allocation.total} allocated")
        stage = STATUS_STAGE.get(job.status)
        if stage and self.previous and self.previous[0] == job.id and self.previous[1] == stage:
            delta = max(0, job.files_completed - self.previous[2]); elapsed = timestamp - self.previous[3]
            self.estimator.observe(WorkSample(stage, delta, elapsed,
                max(0, job.optimized_bytes - self.previous[4]), workers=max(1, allocation.total)))
        self.previous = (job.id, stage, job.files_completed, timestamp, job.optimized_bytes)
        remaining_files = max(0, job.files_total - job.files_completed)
        ordered = list(WorkStage); active_index = ordered.index(stage) if stage in ordered else 0
        workloads = {candidate: {"units": remaining_files if index >= active_index else 0,
                     "bytes": max(0, job.original_bytes - job.optimized_bytes) if index >= active_index else 0,
                     "workers": allocation.total} for index, candidate in enumerate(ordered)}
        overall, estimates = self.estimator.overall(workloads)
        elapsed = 0.0
        if job.started_at: elapsed = max(0, (datetime.now(UTC) - datetime.fromisoformat(job.started_at)).total_seconds())
        stage_etas = {estimate.stage.value: format_duration(estimate.remaining_seconds) for estimate in estimates}
        return DashboardSnapshot(job.target, job.progress, job.files_completed, job.files_total,
            job.original_bytes, job.optimized_bytes, job.current_stage, format_duration(elapsed),
            format_duration(overall), self.estimator.eta_clock(overall), resources.cpu_percent,
            resources.ram_percent, resources.disk_percent, resources.network_down_bps,
            resources.network_up_bps,
            f"{resources.workers_active} active · {allocation.total} allocated · D{allocation.downloads} O{allocation.optimization} U{allocation.uploads}", stage_etas)

    def _history(self) -> dict[WorkStage, float]:
        rates = {}
        with self.repository.connection() as connection:
            rows = connection.execute("""SELECT stage,AVG(
                CASE WHEN julianday(exited_at)>julianday(entered_at)
                THEN 1.0/((julianday(exited_at)-julianday(entered_at))*86400.0) END)
                FROM job_stages WHERE exited_at IS NOT NULL GROUP BY stage""").fetchall()
        for stage_name, rate in rows:
            for status, stage in STATUS_STAGE.items():
                if stage_name == status.value and rate: rates[stage] = float(rate)
        return rates
