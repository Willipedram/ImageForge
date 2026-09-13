"""Primary native desktop window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
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
from app.core.jobs import Job, JobStatus
from app.ui.dashboard import DashboardPage
from app.ui.images_page import ImagesPage
from app.ui.offline_page import OfflinePage
from app.ui.online_page import OnlinePipelinePage
from app.ui.server_page import ServerConnectionPage


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


class JobsPage(QWidget):
    """Persistent history and recovery actions without running work on the UI thread."""

    def __init__(self, engine: JobEngine) -> None:
        super().__init__()
        self.engine = engine
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        heading = QLabel("Jobs & History")
        heading.setObjectName("pageHeading")
        root.addWidget(heading)
        root.addWidget(QLabel("Unfinished jobs are recovered from the durable checkpoint database at startup."))
        self.jobs = QListWidget()
        root.addWidget(self.jobs, 1)
        actions = QHBoxLayout()
        for text, callback in (
            ("Start new job", self._create), ("Resume", self._resume),
            ("Pause", self._pause), ("Cancel", self._cancel), ("Inspect", self._inspect),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            actions.addWidget(button)
        actions.addStretch()
        root.addLayout(actions)
        self.refresh()

    def refresh(self) -> None:
        self.jobs.clear()
        for job in self.engine.repository.list_jobs():
            item = QListWidgetItem(
                f"{job.status.value:<12}  {job.target}  ·  {job.files_completed}/{job.files_total} files  ·  {job.created_at[:19]}"
            )
            item.setData(Qt.UserRole, job.id)
            self.jobs.addItem(item)

    def _selected(self) -> Job | None:
        current = self.jobs.currentItem()
        return self.engine.repository.get(str(current.data(Qt.UserRole))) if current else None

    def _create(self) -> None:
        target, accepted = QInputDialog.getText(self, "New job", "Website or target identifier:")
        if accepted and target.strip():
            try:
                job = self.engine.create_job(target)
                self.engine.transition(job.id, JobStatus.PRECHECK)
            except Exception as exc:
                QMessageBox.warning(self, "Cannot start job", str(exc))
            self.refresh()

    def _resume(self) -> None:
        job = self._selected()
        if job:
            try:
                self.engine.resume(job.id)
            except Exception as exc:
                QMessageBox.warning(self, "Cannot resume job", str(exc))
            self.refresh()

    def _pause(self) -> None:
        job = self._selected()
        if job:
            try:
                self.engine.pause(job.id)
            except Exception as exc:
                QMessageBox.warning(self, "Cannot pause job", str(exc))
            self.refresh()

    def _cancel(self) -> None:
        job = self._selected()
        if job:
            try:
                self.engine.cancel(job.id)
            except Exception as exc:
                QMessageBox.warning(self, "Cannot cancel job", str(exc))
            self.refresh()

    def _inspect(self) -> None:
        job = self._selected()
        if not job:
            return
        checkpoint = self.engine.repository.last_safe_checkpoint(job.id)
        QMessageBox.information(
            self, "Job details",
            f"ID: {job.id}\nTarget: {job.target}\nState: {job.status.value}\n"
            f"Progress: {job.progress:.1f}%\nLast safe checkpoint: "
            f"{checkpoint['operation'] if checkpoint else 'None'}",
        )


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
        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self.log_level.setCurrentText(settings.logging_level)
        form.addRow("ProjectData location", self.data_path)
        form.addRow("Language", self.language)
        form.addRow("Theme", self.theme)
        form.addRow("Optimization profile", self.profile)
        form.addRow("Maximum workers", self.workers)
        form.addRow("Retry attempts", self.retries)
        form.addRow("Maximum image pixels", self.max_pixels)
        form.addRow("Processing timeout", self.timeout)
        form.addRow("Backup retention (days)", self.retention)
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
        self.settings.retry_count = self.retries.value()
        self.settings.optimization_max_pixels = self.max_pixels.value() * 1_000_000
        self.settings.optimization_timeout_seconds = float(self.timeout.value())
        self.settings.backup_retention_days = self.retention.value()
        self.settings.logging_level = self.log_level.currentText()
        self.saved.emit(self.settings)


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings, store: ConfigurationStore, engine: JobEngine | None = None) -> None:
        super().__init__()
        self.store = store
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
        for label in ("Dashboard", "Offline Optimization", "Online Pipeline", "Server Connection", "Images", "Jobs & History", "Logs", "Settings"):
            QListWidgetItem(label, self.navigation)
        side_layout.addWidget(self.navigation, 1)
        version = QLabel("Phase 8  •  Online Pipeline")
        version.setObjectName("versionLabel")
        side_layout.addWidget(version)

        self.pages = QStackedWidget()
        self.dashboard = DashboardPage()
        self.pages.addWidget(self.dashboard)
        if engine:
            self.pages.addWidget(OfflinePage(Path(settings.project_data_path), engine, settings))
        else:
            self.pages.addWidget(PlaceholderPage("Offline Optimization", "Job engine is not connected."))
        self.online_page = OnlinePipelinePage()
        self.pages.addWidget(self.online_page)
        self.pages.addWidget(ServerConnectionPage(Path(settings.project_data_path)))
        if engine:
            from app.database.inventory import InventoryRepository
            self.pages.addWidget(ImagesPage(InventoryRepository(engine.repository)))
        else:
            self.pages.addWidget(PlaceholderPage("Images", "Image inventory is not connected."))
        self.pages.addWidget(
            JobsPage(engine) if engine else PlaceholderPage("Jobs & History", "Job engine is not connected.")
        )
        self.pages.addWidget(PlaceholderPage("Logs", f"Application logs are stored in {Path(settings.project_data_path) / 'logs'}."))
        settings_page = SettingsPage(settings)
        settings_page.saved.connect(self._save_settings)
        self.pages.addWidget(settings_page)
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.navigation.setCurrentRow(0)
        layout.addWidget(sidebar)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(shell)
        self.statusBar().showMessage("Ready  •  ProjectData connected")

    def _save_settings(self, settings: AppSettings) -> None:
        self.store.save(settings)
        self.statusBar().showMessage("Settings saved", 4000)

    def show_error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)
