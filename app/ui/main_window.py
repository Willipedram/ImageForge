"""Primary native desktop window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QApplication,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import AppSettings, ConfigurationStore
from app.core.engine import JobEngine
from app.core.monitoring import ResourceMonitor
from app.core.resources import ResourceLimits, ResourceManager, ResourceProfile
from app.ui.dashboard import DashboardPage
from app.ui.database_page import DatabaseChangesPage
from app.ui.images_page import ImagesPage
from app.ui.server_page import ServerConnectionPage
from app.ui.monitoring import DashboardMonitor
from app.ui.product_pages import BackupsPage, JobsPage as ProductJobsPage, LiveLogsPage, RecoveryPage, ScanPage


class PlaceholderPage(QWidget):
    def __init__(self, title: str, detail: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        heading = QLabel(title)
        heading.setObjectName("pageHeading")
        text = QLabel(detail)
        text.setObjectName("pageSubtitle")
        text.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(text)
        layout.addStretch()


class SettingsPage(QWidget):
    saved = Signal(AppSettings)

    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self.settings = settings
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        heading = QLabel("Settings")
        heading.setObjectName("pageHeading")
        root.addWidget(heading)
        panel = QFrame()
        panel.setObjectName("settingsPanel")
        form = QFormLayout(panel)
        self.data_path = QLabel(settings.project_data_path)
        self.data_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.language = QComboBox()
        self.language.addItem("English", "en")
        self.theme = QComboBox()
        self.theme.addItems(["system", "light", "dark"])
        self.theme.setCurrentText(settings.theme)
        self.profile = QComboBox()
        self.profile.addItems(["safe", "balanced", "aggressive"])
        self.profile.setCurrentText(settings.default_optimization_profile)
        self.workers = QSpinBox()
        self.workers.setRange(1, 64)
        self.workers.setValue(settings.max_workers)
        self.resource_profile = QComboBox()
        self.resource_profile.addItems(["ECO", "BALANCED", "PERFORMANCE", "CUSTOM"])
        self.resource_profile.setCurrentText(settings.resource_profile)
        self.download_workers = QSpinBox(); self.download_workers.setRange(1, 16)
        self.download_workers.setValue(settings.max_download_workers)
        self.upload_workers = QSpinBox(); self.upload_workers.setRange(1, 16)
        self.upload_workers.setValue(settings.max_upload_workers)
        self.server_connections = QSpinBox(); self.server_connections.setRange(1, 16)
        self.server_connections.setValue(settings.max_server_connections)
        self.retries = QSpinBox()
        self.retries.setRange(1, 20)
        self.retries.setValue(settings.retry_count)
        self.max_pixels = QSpinBox()
        self.max_pixels.setRange(1, 1000)
        self.max_pixels.setSuffix(" MP")
        self.max_pixels.setValue(max(1, settings.optimization_max_pixels // 1_000_000))
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 3600)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(round(settings.optimization_timeout_seconds))
        self.retention = QSpinBox()
        self.retention.setRange(1, 3650)
        self.retention.setValue(settings.backup_retention_days)
        self.retention_policy = QComboBox()
        self.retention_policy.addItems(["7_days", "30_days", "90_days", "never"])
        self.retention_policy.setCurrentText(settings.backup_retention_policy)
        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self.log_level.setCurrentText(settings.logging_level)
        form.addRow("ProjectData location", self.data_path)
        form.addRow("Language", self.language)
        form.addRow("Theme", self.theme)
        form.addRow("Optimization profile", self.profile)
        form.addRow("Maximum workers", self.workers)
        form.addRow("Resource profile", self.resource_profile)
        form.addRow("Download workers", self.download_workers)
        form.addRow("Upload workers", self.upload_workers)
        form.addRow("Server connection limit", self.server_connections)
        form.addRow("Retry attempts", self.retries)
        form.addRow("Maximum image pixels", self.max_pixels)
        form.addRow("Processing timeout", self.timeout)
        form.addRow("Backup retention (days)", self.retention)
        form.addRow("Backup retention policy", self.retention_policy)
        form.addRow("Logging level", self.log_level)
        save = QPushButton("Save settings")
        save.setObjectName("primaryButton")
        save.clicked.connect(self._save)
        form.addRow("", save)
        root.addWidget(panel)
        root.addStretch()

    def _save(self) -> None:
        self.settings.ui_language = str(self.language.currentData())
        self.settings.theme = self.theme.currentText()
        self.settings.default_optimization_profile = self.profile.currentText()
        self.settings.max_workers = self.workers.value()
        self.settings.resource_profile = self.resource_profile.currentText()
        self.settings.max_download_workers = self.download_workers.value()
        self.settings.max_upload_workers = self.upload_workers.value()
        self.settings.max_server_connections = self.server_connections.value()
        self.settings.retry_count = self.retries.value()
        self.settings.optimization_max_pixels = self.max_pixels.value() * 1_000_000
        self.settings.optimization_timeout_seconds = float(self.timeout.value())
        self.settings.backup_retention_days = self.retention.value()
        self.settings.backup_retention_policy = self.retention_policy.currentText()
        self.settings.logging_level = self.log_level.currentText()
        self.saved.emit(self.settings)


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings, store: ConfigurationStore, engine: JobEngine | None = None) -> None:
        super().__init__()
        self.store = store
        self._resources = None
        if engine:
            limits = ResourceLimits(settings.max_workers, settings.optimization_max_pixels,
                ResourceProfile(settings.resource_profile), settings.max_download_workers,
                settings.max_workers, settings.max_upload_workers, settings.max_server_connections)
            self._resources = ResourceManager(limits)
        self.setWindowTitle("ImageForge — Website Image Optimization")
        self.resize(1180, 760)
        shell = QWidget()
        layout = QHBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(18, 24, 18, 18)
        brand = QLabel("IMAGEFORGE")
        brand.setObjectName("brand")
        tagline = QLabel("Optimization Suite")
        tagline.setObjectName("tagline")
        side_layout.addWidget(brand)
        side_layout.addWidget(tagline)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        for label in ("Dashboard", "Servers", "Scan", "Images", "Jobs", "Database", "Backups", "Recovery", "Logs", "Settings"):
            QListWidgetItem(label, self.navigation)
        side_layout.addWidget(self.navigation, 1)
        version = QLabel("IMAGEFORGE  •  PROFESSIONAL")
        version.setObjectName("versionLabel")
        side_layout.addWidget(version)

        self.pages = QStackedWidget()
        self.dashboard = DashboardPage()
        self.pages.addWidget(self.dashboard)
        self.server_page = ServerConnectionPage(Path(settings.project_data_path))
        self.pages.addWidget(self.server_page)
        if engine:
            self.scan_page = ScanPage(Path(settings.project_data_path), engine, settings, self._resources)
            self.server_page.connection_verified.connect(self._continue_to_website_scan)
            self.pages.addWidget(self.scan_page)
        else:
            self.pages.addWidget(PlaceholderPage("Scan", "Job engine is not connected."))
        if engine:
            from app.database.inventory import InventoryRepository
            self.pages.addWidget(ImagesPage(InventoryRepository(engine.repository)))
        else:
            self.pages.addWidget(PlaceholderPage("Images", "Image inventory is not connected."))
        self.pages.addWidget(ProductJobsPage(engine, Path(settings.project_data_path)) if engine
                             else PlaceholderPage("Jobs", "Job engine is not connected."))
        self.database_page = DatabaseChangesPage(); self.pages.addWidget(self.database_page)
        self.pages.addWidget(BackupsPage(engine) if engine else PlaceholderPage("Backups", "Backup state is unavailable."))
        self.pages.addWidget(RecoveryPage(engine) if engine else PlaceholderPage("Recovery", "Recovery state is unavailable."))
        self.pages.addWidget(LiveLogsPage(engine) if engine else PlaceholderPage("Logs", "Event stream is unavailable."))
        settings_page = SettingsPage(settings)
        settings_page.saved.connect(self._save_settings)
        self.pages.addWidget(settings_page)
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.navigation.setCurrentRow(0)
        layout.addWidget(sidebar)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(shell)
        self.statusBar().showMessage("Ready  •  ProjectData connected")
        self._monitor_thread = None
        if engine:
            monitor = DashboardMonitor(engine.repository, self._resources,
                ResourceMonitor.system(Path(settings.project_data_path)))
            thread = QThread(self); monitor.moveToThread(thread); thread.started.connect(monitor.run)
            monitor.snapshot.connect(self.dashboard.update_snapshot)
            monitor.failed.connect(lambda message: self.statusBar().showMessage(message, 5000))
            self._monitor_thread, self._monitor = thread, monitor; thread.start()

    def _continue_to_website_scan(self, config, credentials, discovery) -> None:
        """Move the user to the next guided step after connection verification."""
        self.scan_page.online.configure_connection(config, credentials, discovery)
        self.scan_page.tabs.setCurrentWidget(self.scan_page.online)
        self.navigation.setCurrentRow(2)
        self.statusBar().showMessage("Step 2 of 3 — click Scan website", 8000)

    def _save_settings(self, settings: AppSettings) -> None:
        self.store.save(settings)
        from app.ui.theme import stylesheet
        QApplication.instance().setStyleSheet(stylesheet(settings.theme))
        self.statusBar().showMessage("Settings saved", 4000)

    def show_error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

    def closeEvent(self, event) -> None:
        if self._monitor_thread:
            self._monitor.stop(); self._monitor_thread.quit(); self._monitor_thread.wait(1500)
        super().closeEvent(event)
