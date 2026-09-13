"""Bounded scheduling primitives for CPU/memory-heavy local work."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


class ResourceLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    max_workers: int
    max_pixels_per_image: int


class ResourceManager:
    """Caps concurrent decoders and rejects images above the pixel budget."""

    def __init__(self, limits: ResourceLimits) -> None:
        self.limits = limits
        self._slots = threading.BoundedSemaphore(limits.max_workers)

    @contextmanager
    def reserve(self, pixels: int) -> Iterator[None]:
        if pixels > self.limits.max_pixels_per_image:
            raise ResourceLimitExceeded(
                f"Image has {pixels:,} pixels; configured maximum is {self.limits.max_pixels_per_image:,}."
            )
        self._slots.acquire()
        try:
            yield
        finally:
            self._slots.release()
