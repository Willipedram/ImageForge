from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from app.ui.dashboard import DashboardPage, DashboardSnapshot
from app.ui.main_window import MainWindow
from app.ui.theme import stylesheet


def test_main_window_constructs(tmp_path, qapp):
    from app.config.settings import ConfigurationStore
    from app.core.engine import JobEngine
    from app.database.jobs import JobRepository

    store = ConfigurationStore(tmp_path)
    settings = store.load()
    repository = JobRepository(tmp_path / "jobs" / "state.db")
    repository.initialize()
    window = MainWindow(settings, store, JobEngine(repository))
    assert window.windowTitle().startswith("ImageForge")
    assert window.pages.count() == 10
    assert [window.navigation.item(index).text() for index in range(window.navigation.count())] == [
        "Dashboard", "Servers", "Scan", "Images", "Jobs", "Database", "Backups", "Recovery", "Logs", "Settings"
    ]
    assert "files_remaining" in window.dashboard.cards and "network" in window.dashboard.cards
    window.close()


def test_dashboard_accepts_mocked_live_job_snapshot(qapp):
    page = DashboardPage()
    page.update_snapshot(DashboardSnapshot("Job #104 · shop.test", 72, 1240, 1720,
        100_000, 61_000, "Downloading", "00:18:42", "00:07:31", "14:32:18",
        54, 63, 41, 2_000_000, 500_000, "4 active · 6 allocated",
        {"Download": "00:07:31", "Optimize": "00:12:10"}))
    assert page.progress.value() == 72
    assert page.cards["files_remaining"].value_label.text() == "480"
    assert page.cards["reduction"].value_label.text() == "39.0%"
    assert page.stage_labels["Download"].text().endswith("00:07:31")


def test_theme_system_produces_distinct_professional_palettes():
    assert stylesheet("light") != stylesheet("dark")
    assert "Segoe UI" in stylesheet("system") and "QTableWidget" in stylesheet("dark")
