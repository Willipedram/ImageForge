from __future__ import annotations

from app.core.estimation import TimeEstimator, WorkSample, WorkStage, format_duration
from app.core.monitoring import ResourceMonitor
from app.core.resources import ResourceLimits, ResourceManager, ResourceProfile, ResourceSnapshot


class Provider:
    def __init__(self): self.received = 1000; self.sent = 500
    def cpu_percent(self): return 42.0
    def ram_percent(self): return 61.0
    def disk_percent(self, path): return 37.0
    def network_bytes(self): return self.received, self.sent


def test_eta_starts_calculating_then_uses_actual_samples_and_worker_complexity():
    estimator = TimeEstimator()
    initial = estimator.estimate(WorkStage.OPTIMIZE, 10)
    assert initial.remaining_seconds is None and format_duration(initial.remaining_seconds) == "Calculating ETA..."
    estimator.observe(WorkSample(WorkStage.OPTIMIZE, 5, 10, pixels_processed=20_000_000, workers=2))
    estimate = estimator.estimate(WorkStage.OPTIMIZE, 10, remaining_pixels=40_000_000, workers=2)
    assert estimate.remaining_seconds == 40 and estimate.samples == 1
    assert format_duration(estimate.remaining_seconds) == "00:00:40"


def test_stage_and_overall_eta_require_real_or_historical_rates():
    estimator = TimeEstimator({WorkStage.SCAN: 2.0})
    overall, stages = estimator.overall({WorkStage.SCAN: {"units": 10}, WorkStage.UPLOAD: {"units": 4}})
    assert overall is None and stages[0].remaining_seconds == 5
    estimator.observe(WorkSample(WorkStage.UPLOAD, 2, 4))
    overall, _ = estimator.overall({WorkStage.SCAN: {"units": 10}, WorkStage.UPLOAD: {"units": 4}})
    assert overall == 13


def test_resource_monitor_calculates_network_rates_from_samples(tmp_path):
    provider = Provider(); monitor = ResourceMonitor(provider, tmp_path)
    first = monitor.sample(now=10); provider.received += 4000; provider.sent += 2000
    second = monitor.sample(workers_active=3, now=12)
    assert first.network_down_bps == 0
    assert (second.cpu_percent, second.ram_percent, second.disk_percent) == (42, 61, 37)
    assert second.network_down_bps == 2000 and second.network_up_bps == 1000 and second.workers_active == 3


def test_adaptive_workers_reduce_under_pressure_and_respect_connection_cap():
    manager = ResourceManager(ResourceLimits(10, 80_000_000, ResourceProfile.PERFORMANCE,
        max_download_workers=4, max_optimization_workers=6, max_upload_workers=4, max_server_connections=3))
    initial = manager.allocation
    assert initial.downloads + initial.uploads <= 3 and initial.total <= 10
    pressured = manager.adapt(ResourceSnapshot(99, 92, 40, 0, 0))
    assert pressured.optimization < initial.optimization and pressured.total <= initial.total
    recovering = manager.adapt(ResourceSnapshot(20, 40, 40, 10_000, 10_000))
    assert recovering.optimization == pressured.optimization + 1


def test_profiles_default_to_balanced_and_eco_is_smaller():
    balanced = ResourceManager(ResourceLimits(12, 1, max_optimization_workers=12)).allocation
    eco = ResourceManager(ResourceLimits(12, 1, ResourceProfile.ECO, max_optimization_workers=12)).allocation
    assert balanced.optimization > eco.optimization
