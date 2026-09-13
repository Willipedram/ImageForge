"""Low-overhead process/system telemetry sampling."""

from __future__ import annotations

import importlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.resources import ResourceSnapshot


class MetricsProvider(Protocol):
    def cpu_percent(self) -> float: ...
    def ram_percent(self) -> float: ...
    def disk_percent(self, path: Path) -> float: ...
    def network_bytes(self) -> tuple[int, int]: ...


class PsutilMetricsProvider:
    def __init__(self) -> None: self.psutil = importlib.import_module("psutil")
    def cpu_percent(self) -> float: return float(self.psutil.cpu_percent(interval=None))
    def ram_percent(self) -> float: return float(self.psutil.virtual_memory().percent)
    def disk_percent(self, path: Path) -> float: return float(self.psutil.disk_usage(str(path)).percent)
    def network_bytes(self) -> tuple[int, int]:
        counters = self.psutil.net_io_counters(); return int(counters.bytes_recv), int(counters.bytes_sent)


@dataclass(slots=True)
class ResourceMonitor:
    provider: MetricsProvider
    project_data: Path
    _last_time: float | None = None
    _last_received: int = 0
    _last_sent: int = 0

    @classmethod
    def system(cls, project_data: Path) -> "ResourceMonitor":
        return cls(PsutilMetricsProvider(), project_data)

    def sample(self, *, workers_active: int = 0, now: float | None = None) -> ResourceSnapshot:
        timestamp = now if now is not None else time.monotonic()
        received, sent = self.provider.network_bytes()
        elapsed = timestamp - self._last_time if self._last_time is not None else 0
        down = max(0, received - self._last_received) / elapsed if elapsed > 0 else 0.0
        up = max(0, sent - self._last_sent) / elapsed if elapsed > 0 else 0.0
        self._last_time, self._last_received, self._last_sent = timestamp, received, sent
        return ResourceSnapshot(self.provider.cpu_percent(), self.provider.ram_percent(),
            self.provider.disk_percent(self.project_data), down, up, workers_active)
