from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")


def test_main_window_constructs(tmp_path, qapp):
    from app.config.settings import ConfigurationStore
    from app.core.engine import JobEngine
    from app.database.jobs import JobRepository
    from app.ui.main_window import MainWindow

    store = ConfigurationStore(tmp_path)
    settings = store.load()
    repository = JobRepository(tmp_path / "jobs" / "state.db")
    repository.initialize()
    window = MainWindow(settings, store, JobEngine(repository))
    assert window.windowTitle().startswith("ImageForge")
    assert window.pages.count() == 7
    window.close()
