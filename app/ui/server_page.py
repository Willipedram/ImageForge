"""Server connection and discovery UI backed by a QThread worker."""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

from app.server import ConnectionConfig, Protocol, RuntimeCredentials, create_server
from app.server.credentials import CredentialProvider, RuntimeCredentialProvider, WindowsCredentialProvider
from app.server.preflight import PreflightReport, PreflightService
from app.utils.checkpoints import log_keypoint

logger = logging.getLogger(__name__)


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
        server = None
        try:
            server = create_server(self.config, self.credentials)
            host = self.config.host.casefold()
            for prefix in ("ftp.", "www."):
                if host.startswith(prefix):
                    host = host[len(prefix):]
            common_roots = (
                "/",
                f"/domains/{host}/public_html",
                "/public_html",
                "/private_html",
                "/www",
                "/htdocs",
            )
            self.finished.emit(PreflightService(server, self.project_data).run(
                self.config.remote_root, common_roots
            ))
        except Exception as exc:
            log_keypoint(logger, "connection_worker", "stopped", error=exc)
            self.failed.emit(str(exc))
        finally:
            if server is not None:
                server.disconnect()
            self.credentials = RuntimeCredentials("", "")


class ServerConnectionPage(QWidget):
    connection_verified = Signal(object, object, object)

    def __init__(self, project_data: Path, credential_provider: CredentialProvider | None = None) -> None:
        super().__init__()
        self.project_data = project_data
        self._thread: QThread | None = None
        self._pending_credentials: RuntimeCredentials | None = None
        self._pending_config: ConnectionConfig | None = None
        if credential_provider is not None:
            self.credential_provider = credential_provider
        elif sys.platform == "win32":
            try:
                self.credential_provider = WindowsCredentialProvider()
            except (ImportError, OSError):
                self.credential_provider = RuntimeCredentialProvider()
        else:
            self.credential_provider = RuntimeCredentialProvider()
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        heading = QLabel("Server Connection")
        heading.setObjectName("pageHeading")
        root.addWidget(heading)
        guide = QLabel("Step 1 of 3 — enter FTP details and verify the WordPress site")
        guide.setObjectName("pageSubtitle")
        root.addWidget(guide)
        root.addWidget(QLabel("Test secure access and discover the website without downloading image bodies."))
        access_note = QLabel(
            "DirectAdmin panel access is not a file-transfer connection. Use an FTP/FTPS account "
            "created in DirectAdmin (usually port 21), or an SSH/SFTP account enabled by the host."
        )
        access_note.setWordWrap(True)
        access_note.setObjectName("pageSubtitle")
        root.addWidget(access_note)
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
        self.password.setPlaceholderText("Password")
        self.remote_root = QLineEdit("/")
        self.remote_root.setPlaceholderText("Example: /domains/example.com/public_html")
        self.remember_password = QCheckBox("Save password securely in Windows Credential Manager")
        self.remember_password.setEnabled(not isinstance(self.credential_provider, RuntimeCredentialProvider))
        self.remember_password.toggled.connect(self._remember_toggled)
        self.remember_password.setChecked(self.remember_password.isEnabled())
        self.host.editingFinished.connect(self._load_saved_credentials)
        self.strict_security = QCheckBox("Verify TLS certificate / SSH host key")
        self.strict_security.setChecked(True)
        self.protocol.currentTextChanged.connect(self._protocol_changed)
        for label, widget in (
            ("Protocol", self.protocol), ("Host", self.host), ("Port", self.port),
            ("Username", self.username), ("Password", self.password),
            ("Remote root", self.remote_root), ("Security", self.strict_security),
            ("Credentials", self.remember_password),
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
        if self.remember_password.isChecked() and not self.password.text():
            self._load_saved_credentials()
        if not self.host.text().strip() or not self.username.text().strip():
            self.results.setPlainText("Host and username are required.")
            return
        protocol = Protocol(self.protocol.currentText())
        config = ConnectionConfig(
            protocol, self.host.text().strip(), self.port.value(), self.remote_root.text().strip() or "/",
            verify_tls=self.strict_security.isChecked(), verify_host_key=self.strict_security.isChecked(),
        )
        credentials = RuntimeCredentials(self.username.text(), self.password.text())
        self._pending_config = config
        self._pending_credentials = credentials
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
        if any(check.name == "Remote access" and not check.passed for check in report.checks):
            lines.extend(["", *self._connection_help(),
                          f"Failure keypoint was written to: {self.project_data / 'logs' / 'imageforge.log'}"])
        connected = any(check.name == "Connection and authentication" and check.passed for check in report.checks)
        if connected and self.remember_password.isChecked() and self._pending_credentials:
            try:
                self.credential_provider.save(self._credential_id(), self._pending_credentials)
                lines.extend(["", "✓ Password saved securely in Windows Credential Manager"])
            except Exception as exc:
                lines.extend(["", f"✕ Password was not saved: {exc}"])
        if report.discovery:
            self.remote_root.setText(report.discovery.site_root or report.discovery.search_root)
        if report.discovery and report.discovery.wordpress:
            site = report.discovery
            if self._pending_config and self._pending_credentials:
                self.connection_verified.emit(self._pending_config, self._pending_credentials, site)
            lines.extend([
                "", "WordPress discovered",
                f"Site root: {site.site_root}", f"Content: {site.wp_content}",
                f"Uploads: {site.uploads or 'Not found'}", f"Themes: {site.themes or 'Not found'}",
                f"Plugins: {site.plugins or 'Not found'}",
                f"WooCommerce: {'Yes' if site.woocommerce else 'No'}",
                f"Elementor: {'Yes' if site.elementor else 'No'}",
                "", "✓ Website scanner configured — open Scan > Website and click Scan website.",
            ])
        elif report.discovery:
            lines.extend(["", *self._discovery_help(report.discovery)])
        self.results.setPlainText("\n".join(lines))

    def _connection_help(self) -> list[str]:
        if self.port.value() in {2222, 2223}:
            return [
                f"DirectAdmin port {self.port.value()} is for the web control panel, not FTP/SFTP.",
                "Create or select an FTP account in DirectAdmin, then choose FTP/FTPS and port 21 here.",
            ]
        if self.protocol.currentText() == Protocol.SFTP.value:
            return [
                "SFTP requires SSH access enabled by the hosting provider; DirectAdmin access alone is not enough.",
                "If SSH is unavailable, create an FTP account in DirectAdmin and try FTP/FTPS on port 21.",
            ]
        return [
            "Check the FTP hostname, port, username, and password shown in DirectAdmin's FTP Management page.",
            "The DirectAdmin web-panel URL/password is not automatically an FTP login.",
        ]

    def _discovery_help(self, discovery=None) -> list[str]:
        lines = [
            "DIAGNOSIS: FTP authentication works, but no accessible directory contains a WordPress root.",
            "In DirectAdmin > FTP Management, set this account to Domain directory access and use /public_html here,",
            "or set its custom path to the domain's public_html directory. A public_html subdirectory account cannot scan the whole site.",
            "Use the closest path and missing markers shown above to confirm exactly what this FTP account can see.",
        ]
        if discovery and "/public_html" in discovery.empty_directories:
            lines.extend([
                "EVIDENCE: /public_html is visible but empty for this FTP account.",
                "ROOT CAUSE: WordPress files are outside the account root, or are not installed in this domain's document root.",
                "Open DirectAdmin File Manager, locate wp-config.php, then grant this FTP account access to that exact parent directory.",
                "SUPPORT REQUEST: Please set this FTP user's root to the directory containing wp-config.php; /public_html currently returns 0 entries.",
            ])
        return lines

    def _credential_id(self) -> str:
        endpoint = f"{self.protocol.currentText()}|{self.host.text().strip().casefold()}|{self.port.value()}"
        return "server-" + hashlib.sha256(endpoint.encode("utf-8")).hexdigest()

    @Slot(bool)
    def _remember_toggled(self, checked: bool) -> None:
        if not self.host.text().strip():
            return
        try:
            if checked:
                self._load_saved_credentials()
            else:
                self.credential_provider.delete(self._credential_id())
        except Exception as exc:
            self.results.setPlainText(f"Credential Manager error: {exc}")

    @Slot()
    def _load_saved_credentials(self) -> None:
        if not self.remember_password.isChecked() or not self.host.text().strip():
            return
        try:
            saved = self.credential_provider.load(self._credential_id())
            if saved:
                self.username.setText(saved.username)
                self.password.setText(saved.password)
        except Exception as exc:
            self.results.setPlainText(f"Credential Manager error: {exc}")

    @Slot(str)
    def _show_error(self, message: str) -> None:
        self.results.setPlainText("\n".join([
            f"Connection failed: {message}", "", *self._connection_help(),
            f"Failure keypoint was written to: {self.project_data / 'logs' / 'imageforge.log'}",
        ]))

    @Slot()
    def _finished(self) -> None:
        self._pending_credentials = None
        self.test_button.setEnabled(True)
        self._thread = None
