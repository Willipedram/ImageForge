"""Server connection and discovery UI backed by a QThread worker."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

from app.server import ConnectionConfig, Protocol, RuntimeCredentials, create_server
from app.server.preflight import PreflightReport, PreflightService


class DiscoveryWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, config: ConnectionConfig, credentials: RuntimeCredentials, project_data: Path) -> None:
        super().__init__()
        self.config = config
        self.credentials = credentials
        self.project_data = project_data

    @Slot()
    def run(self) -> None:
        server = create_server(self.config, self.credentials)
        try:
            self.finished.emit(PreflightService(server, self.project_data).run(self.config.remote_root))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            server.disconnect()
            self.credentials = RuntimeCredentials("", "")


class ServerConnectionPage(QWidget):
    def __init__(self, project_data: Path) -> None:
        super().__init__()
        self.project_data = project_data
        self._thread: QThread | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        heading = QLabel("Server Connection")
        heading.setObjectName("pageHeading")
        root.addWidget(heading)
        root.addWidget(QLabel("Test secure access and discover the website without downloading image bodies."))
        panel = QFrame()
        panel.setObjectName("settingsPanel")
        form = QFormLayout(panel)
        self.protocol = QComboBox()
        self.protocol.addItems([member.value for member in Protocol])
        self.host = QLineEdit()
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("Used for this connection only")
        self.remote_root = QLineEdit("/")
        self.strict_security = QCheckBox("Verify TLS certificate / SSH host key")
        self.strict_security.setChecked(True)
        self.protocol.currentTextChanged.connect(self._protocol_changed)
        for label, widget in (
            ("Protocol", self.protocol), ("Host", self.host), ("Port", self.port),
            ("Username", self.username), ("Password", self.password),
            ("Remote root", self.remote_root), ("Security", self.strict_security),
        ):
            form.addRow(label, widget)
        buttons = QHBoxLayout()
        self.test_button = QPushButton("Test & discover")
        self.test_button.setObjectName("primaryButton")
        self.test_button.clicked.connect(self._start)
        buttons.addWidget(self.test_button)
        buttons.addStretch()
        form.addRow("", buttons)
        root.addWidget(panel)
        self.results = QTextEdit()
        self.results.setReadOnly(True)
        self.results.setPlaceholderText("Discovery results will appear here.")
        root.addWidget(self.results, 1)

    def _protocol_changed(self, value: str) -> None:
        self.port.setValue(22 if value == Protocol.SFTP.value else 21)

    def _start(self) -> None:
        if self._thread and self._thread.isRunning():
            return
        if not self.host.text().strip() or not self.username.text().strip():
            self.results.setPlainText("Host and username are required.")
            return
        protocol = Protocol(self.protocol.currentText())
        config = ConnectionConfig(
            protocol, self.host.text().strip(), self.port.value(), self.remote_root.text().strip() or "/",
            verify_tls=self.strict_security.isChecked(), verify_host_key=self.strict_security.isChecked(),
        )
        credentials = RuntimeCredentials(self.username.text(), self.password.text())
        self.password.clear()
        self.test_button.setEnabled(False)
        self.results.setPlainText("Connecting and discovering…")
        thread = QThread(self)
        worker = DiscoveryWorker(config, credentials, self.project_data)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._show_report)
        worker.failed.connect(self._show_error)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._finished)
        self._thread = thread
        self._worker = worker
        thread.start()

    @Slot(object)
    def _show_report(self, report: PreflightReport) -> None:
        lines = [f"{'✓' if check.passed else '✕'} {check.name}: {check.detail}" for check in report.checks]
        if report.discovery and report.discovery.wordpress:
            site = report.discovery
            lines.extend([
                "", "WordPress discovered",
                f"Site root: {site.site_root}", f"Content: {site.wp_content}",
                f"Uploads: {site.uploads or 'Not found'}", f"Themes: {site.themes or 'Not found'}",
                f"Plugins: {site.plugins or 'Not found'}",
                f"WooCommerce: {'Yes' if site.woocommerce else 'No'}",
                f"Elementor: {'Yes' if site.elementor else 'No'}",
            ])
        self.results.setPlainText("\n".join(lines))

    @Slot(str)
    def _show_error(self, message: str) -> None:
        self.results.setPlainText(f"Connection failed: {message}")

    @Slot()
    def _finished(self) -> None:
        self.test_button.setEnabled(True)
        self._thread = None
