"""Bounded and adaptive scheduling primitives for independent workload pools."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterator


class ResourceLimitExceeded(RuntimeError):
    pass


class ResourceProfile(StrEnum):
    ECO = "ECO"
    BALANCED = "BALANCED"
    PERFORMANCE = "PERFORMANCE"
    CUSTOM = "CUSTOM"


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    max_workers: int
    max_pixels_per_image: int
    profile: ResourceProfile = ResourceProfile.BALANCED
    max_download_workers: int = 2
    max_optimization_workers: int = 4
    max_upload_workers: int = 2
    max_server_connections: int = 3
    ram_pressure_percent: float = 85.0
    cpu_target_percent: float = 80.0


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    cpu_percent: float
    ram_percent: float
    disk_percent: float
    network_down_bps: float
    network_up_bps: float
    workers_active: int = 0


@dataclass(frozen=True, slots=True)
class WorkerAllocation:
    downloads: int
    optimization: int
    uploads: int

    @property
    def total(self) -> int: return self.downloads + self.optimization + self.uploads


class ResourceManager:
    """Caps decoders and computes conservative pool sizes from measured pressure."""

    def __init__(self, limits: ResourceLimits) -> None:
        self.limits = limits
        self._lock = threading.Lock()
        self._allocation = self._profile_allocation()
        self._gates = {"download": _AdaptiveGate(self._allocation.downloads),
                       "optimize": _AdaptiveGate(self._allocation.optimization),
                       "upload": _AdaptiveGate(self._allocation.uploads)}
        self._connections = _AdaptiveGate(limits.max_server_connections)
        self._global = _AdaptiveGate(limits.max_workers)

    @property
    def allocation(self) -> WorkerAllocation:
        with self._lock: return self._allocation

    @property
    def active_workers(self) -> int:
        return self._global.active_count

    def adapt(self, snapshot: ResourceSnapshot) -> WorkerAllocation:
        """Reduce rapidly under pressure and grow by one worker to avoid oscillation."""
        with self._lock:
            current = self._allocation
            if snapshot.ram_percent >= self.limits.ram_pressure_percent or snapshot.cpu_percent >= 96:
                optimization = max(1, current.optimization // 2)
                downloads = max(1, current.downloads - 1)
                uploads = max(1, current.uploads - 1)
            elif snapshot.cpu_percent < self.limits.cpu_target_percent - 15 and snapshot.ram_percent < self.limits.ram_pressure_percent - 10:
                optimization = min(self.limits.max_optimization_workers, current.optimization + 1)
                downloads = min(self.limits.max_download_workers, current.downloads + int(snapshot.network_down_bps > 0))
                uploads = min(self.limits.max_upload_workers, current.uploads + int(snapshot.network_up_bps > 0))
            else:
                downloads, optimization, uploads = current.downloads, current.optimization, current.uploads
            downloads, uploads = self._cap_connections(downloads, uploads)
            while downloads + optimization + uploads > self.limits.max_workers:
                if optimization > 1: optimization -= 1
                elif downloads > 1: downloads -= 1
                elif uploads > 1: uploads -= 1
                else: break
            self._allocation = WorkerAllocation(downloads, optimization, uploads)
            self._gates["download"].resize(downloads); self._gates["optimize"].resize(optimization)
            self._gates["upload"].resize(uploads)
            return self._allocation

    @contextmanager
    def reserve(self, pixels: int) -> Iterator[None]:
        if pixels > self.limits.max_pixels_per_image:
            raise ResourceLimitExceeded(
                f"Image has {pixels:,} pixels; configured maximum is {self.limits.max_pixels_per_image:,}."
            )
        with self.reserve_pool("optimize"):
            yield

    @contextmanager
    def reserve_pool(self, pool: str) -> Iterator[None]:
        """Reserve a pool slot; network pools also share the server-connection cap."""
        if pool not in self._gates: raise ValueError("pool must be download, optimize, or upload")
        network = pool in {"download", "upload"}
        with self._gates[pool].reserve():
            with self._global.reserve():
                if network:
                    with self._connections.reserve(): yield
                else: yield

    def _profile_allocation(self) -> WorkerAllocation:
        scale = {ResourceProfile.ECO: .35, ResourceProfile.BALANCED: .65,
                 ResourceProfile.PERFORMANCE: 1.0, ResourceProfile.CUSTOM: 1.0}[self.limits.profile]
        optimization = max(1, min(self.limits.max_optimization_workers, round(self.limits.max_workers * scale)))
        downloads = max(1, round(self.limits.max_download_workers * scale))
        uploads = max(1, round(self.limits.max_upload_workers * scale))
        downloads, uploads = self._cap_connections(downloads, uploads)
        while downloads + optimization + uploads > self.limits.max_workers and optimization > 1: optimization -= 1
        return WorkerAllocation(downloads, optimization, uploads)

    def _cap_connections(self, downloads: int, uploads: int) -> tuple[int, int]:
        while downloads + uploads > self.limits.max_server_connections:
            if downloads >= uploads and downloads > 1: downloads -= 1
            elif uploads > 1: uploads -= 1
            else: break
        return downloads, uploads


class _AdaptiveGate:
    """Resizable scheduling gate; reductions wait for active work to finish naturally."""
    def __init__(self, limit: int) -> None:
        self.limit, self.active = max(1, limit), 0; self.condition = threading.Condition()
    def resize(self, limit: int) -> None:
        with self.condition: self.limit = max(1, limit); self.condition.notify_all()
    @property
    def active_count(self) -> int:
        with self.condition: return self.active
    @contextmanager
    def reserve(self):
        with self.condition:
            while self.active >= self.limit: self.condition.wait()
            self.active += 1
        try: yield
        finally:
            with self.condition: self.active -= 1; self.condition.notify()
