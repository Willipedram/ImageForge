from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from app.ui.dashboard import DashboardPage, DashboardSnapshot
from app.ui.main_window import MainWindow
from app.ui.server_page import ServerConnectionPage
from app.ui.theme import stylesheet
from app.server.base import RuntimeCredentials
from app.server.base import ConnectionConfig, Protocol
from app.server.discovery import SiteDiscovery


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
    window.server_page.connection_verified.emit(
        ConnectionConfig(Protocol.FTP, "example.test", 21),
        RuntimeCredentials("user", "secret"),
        SiteDiscovery("/", "/", True, uploads="/wp-content/uploads"),
    )
    assert window.scan_page.online.stage.text().startswith("Ready to scan")
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


def test_server_page_loads_saved_windows_credentials(tmp_path, qapp):
    class CredentialProvider:
        def load(self, credential_id):
            return RuntimeCredentials("saved-user", "saved-password")
        def save(self, credential_id, credentials):
            self.saved = (credential_id, credentials)
        def delete(self, credential_id):
            self.deleted = credential_id

    page = ServerConnectionPage(tmp_path, CredentialProvider())
    assert page.remember_password.isChecked()
    page.host.setText("safirezaman.com")
    page._load_saved_credentials()
    assert page.username.text() == "saved-user"
    assert page.password.text() == "saved-password"


def test_server_page_explains_directadmin_is_not_sftp(tmp_path, qapp):
    page = ServerConnectionPage(tmp_path)
    page.port.setValue(2223)
    assert "web control panel" in " ".join(page._connection_help())
    page.port.setValue(22)
    page.protocol.setCurrentText("SFTP")
    assert "SSH access" in " ".join(page._connection_help())
    assert "Domain directory" in " ".join(page._discovery_help())
    assert "subdirectory account" in " ".join(page._discovery_help())
    assert "visible but empty" in " ".join(page._discovery_help(
        SimpleNamespace(empty_directories=("/public_html",))
    ))
    assert "ROOT CAUSE" in " ".join(page._discovery_help(
        SimpleNamespace(empty_directories=("/public_html",))
    ))


def test_theme_system_produces_distinct_professional_palettes():
    assert stylesheet("light") != stylesheet("dark")
    assert "Segoe UI" in stylesheet("system") and "QTableWidget" in stylesheet("dark")
