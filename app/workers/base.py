"""Cooperative worker base; actual processing belongs to later phases."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot


class CooperativeWorker(QObject):
    progress = Signal(float, str)
    finished = Signal()
    failed = Signal(str)
    paused = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._cancel_requested = False
        self._paused = False

    @Slot()
    def request_cancel(self) -> None:
        self._cancel_requested = True

    @Slot()
    def pause(self) -> None:
        self._paused = True
        self.paused.emit(True)

    @Slot()
    def resume(self) -> None:
        self._paused = False
        self.paused.emit(False)

    @property
    def should_cancel(self) -> bool:
        return self._cancel_requested

    @property
    def is_paused(self) -> bool:
        return self._paused
