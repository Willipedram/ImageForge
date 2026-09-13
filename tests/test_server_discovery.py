from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.retry import RetryPolicy
from app.server.base import ConnectionConfig, Protocol, RemoteEntry, RemoteServer, RuntimeCredentials
from app.server.credentials import RuntimeCredentialProvider
from app.server.discovery import DiscoveryOptions, RemoteImageScanner, SiteDiscoverer
from app.server.paths import UnsafeRemotePath, normalize_remote_path, safe_join
from app.server.preflight import PreflightService
from app.server.resilience import ResilientServer
from app.server.testing import ConnectionTester


class FakeServer(RemoteServer):
    def __init__(self, tree, fail_lists=0):
        self.tree = tree
        self._connected = False
        self.fail_lists = fail_lists
        self.download_count = 0
        self.prefix_count = 0
        self.files: dict[str, bytes] = {}

    @property
    def connected(self): return self._connected
    def connect(self): self._connected = True
    def disconnect(self): self._connected = False

    def list(self, path):
        if not self.connected:
            raise ConnectionError("disconnected")
        if self.fail_lists:
            self.fail_lists -= 1
            raise ConnectionError("temporary disconnect")
        return self.tree.get(normalize_remote_path(path), [])

    def stat(self, path):
        path = normalize_remote_path(path)
        if path in self.tree:
            return RemoteEntry(path, path.rsplit("/", 1)[-1] or "/", True)
        for entries in self.tree.values():
            for entry in entries:
                if entry.path == path:
                    return entry
        if path in self.files:
            return RemoteEntry(path, path.rsplit("/", 1)[-1], False, len(self.files[path]))
        raise FileNotFoundError(path)

    def download(self, remote_path, destination):
        self.download_count += 1
        data = self.files[normalize_remote_path(remote_path)]
        if isinstance(destination, Path): destination.write_bytes(data)
        else: destination.write(data)

    def upload(self, source, remote_path):
        data = source.read() if not isinstance(source, Path) else source.read_bytes()
        self.files[normalize_remote_path(remote_path)] = data

    def delete(self, path): self.files.pop(normalize_remote_path(path))
    def rename(self, source, destination): self.files[normalize_remote_path(destination)] = self.files.pop(normalize_remote_path(source))
    def exists(self, path):
        try: self.stat(path); return True
        except FileNotFoundError: return False
    def mkdir(self, path): self.tree[normalize_remote_path(path)] = []
    def checksum(self, path, algorithm="sha256"): return hashlib.new(algorithm, self.files[normalize_remote_path(path)]).hexdigest()
    def read_prefix(self, path, maximum_bytes):
        self.prefix_count += 1
        return self.files.get(normalize_remote_path(path), b"")[:maximum_bytes]


def directory(path, name, symlink=False):
    return RemoteEntry(path, name, True, is_symlink=symlink)


def file(path, name, size=10):
    return RemoteEntry(path, name, False, size, datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def wordpress_server():
    tree = {
        "/": [directory("/clients", "clients")],
        "/clients": [directory("/clients/Acme Site", "Acme Site")],
        "/clients/Acme Site": [
            file("/clients/Acme Site/wp-config.php", "wp-config.php"),
            directory("/clients/Acme Site/wp-admin", "wp-admin"),
            directory("/clients/Acme Site/wp-content", "wp-content"),
            directory("/clients/Acme Site/wp-includes", "wp-includes"),
        ],
        "/clients/Acme Site/wp-admin": [],
        "/clients/Acme Site/wp-includes": [],
        "/clients/Acme Site/wp-content": [
            directory("/clients/Acme Site/wp-content/uploads", "uploads"),
            directory("/clients/Acme Site/wp-content/themes", "themes"),
            directory("/clients/Acme Site/wp-content/plugins", "plugins"),
        ],
        "/clients/Acme Site/wp-content/plugins": [
            directory("/clients/Acme Site/wp-content/plugins/woocommerce", "woocommerce"),
            directory("/clients/Acme Site/wp-content/plugins/elementor", "elementor"),
        ],
        "/clients/Acme Site/wp-content/uploads": [
            file("/clients/Acme Site/wp-content/uploads/café hero.JPG", "café hero.JPG", 120),
            file("/clients/Acme Site/wp-content/uploads/Hero.jpg", "Hero.jpg", 90),
            file("/clients/Acme Site/wp-content/uploads/hero.jpg", "hero.jpg", 80),
            file("/clients/Acme Site/wp-content/uploads/readme.txt", "readme.txt", 20),
            directory("/outside", "unsafe-link", symlink=True),
        ],
        "/clients/Acme Site/wp-content/themes": [],
        "/clients/Acme Site/wp-content/plugins/woocommerce": [],
        "/clients/Acme Site/wp-content/plugins/elementor": [],
    }
    server = FakeServer(tree)
    server.files["/clients/Acme Site/wp-content/uploads/café hero.JPG"] = b""
    server.files["/clients/Acme Site/wp-content/uploads/Hero.jpg"] = b""
    server.files["/clients/Acme Site/wp-content/uploads/hero.jpg"] = b""
    server.connect()
    return server


def test_heuristic_wordpress_and_plugin_discovery(wordpress_server):
    result = SiteDiscoverer(wordpress_server).discover("/")
    assert result.wordpress
    assert result.site_root == "/clients/Acme Site"
    assert result.uploads.endswith("/uploads")
    assert result.woocommerce and result.elementor


def test_discovery_supports_configurable_content_and_upload_names():
    tree = {
        "/site": [file("/site/index.php", "index.php"), directory("/site/wp-admin", "wp-admin"),
                  directory("/site/wp-includes", "wp-includes"), directory("/site/assets", "assets")],
        "/site/wp-admin": [], "/site/wp-includes": [],
        "/site/assets": [directory("/site/assets/pictures", "pictures")],
        "/site/assets/pictures": [],
    }
    server = FakeServer(tree)
    server.connect()
    options = DiscoveryOptions(content_names=("assets",), uploads_names=("pictures",))
    result = SiteDiscoverer(server, options).discover("/site")
    assert result.wordpress and result.wp_content == "/site/assets"
    assert result.uploads == "/site/assets/pictures"


def test_metadata_scan_handles_unicode_spaces_and_case_without_downloads(wordpress_server):
    images = list(RemoteImageScanner(wordpress_server).scan("/clients/Acme Site/wp-content/uploads"))
    assert [image.path for image in images] == [
        "/clients/Acme Site/wp-content/uploads/café hero.JPG",
        "/clients/Acme Site/wp-content/uploads/Hero.jpg",
        "/clients/Acme Site/wp-content/uploads/hero.jpg",
    ]
    assert images[0].extension == ".jpg"
    assert wordpress_server.download_count == 0
    assert wordpress_server.prefix_count == 3


def test_dimensions_are_read_from_bounded_headers_without_full_download():
    path = "/uploads/picture.png"
    png_header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (640).to_bytes(4, "big") + (480).to_bytes(4, "big")
    server = FakeServer({"/uploads": [file(path, "picture.png", 5_000_000)]})
    server.files[path] = png_header + b"remaining body"
    server.connect()
    image = next(RemoteImageScanner(server).scan("/uploads"))
    assert (image.width, image.height) == (640, 480)
    assert server.download_count == 0


def test_remote_path_traversal_and_special_characters():
    assert safe_join("/sites/Client Name", "images", "café 1.png") == "/sites/Client Name/images/café 1.png"
    with pytest.raises(UnsafeRemotePath):
        safe_join("/sites/client", "../other/secret")
    with pytest.raises(UnsafeRemotePath):
        normalize_remote_path("../../etc/passwd")


def test_resilient_server_disconnects_and_reconnects():
    server = FakeServer({"/": []}, fail_lists=1)
    delays = []
    resilient = ResilientServer(server, RetryPolicy(max_attempts=2, base_delay_seconds=0), delays.append)
    assert resilient.call(lambda: server.list("/")) == []
    assert delays == [0]
    assert server.connected


def test_connection_permissions_and_safe_write_cleanup():
    server = FakeServer({"/": []})
    results = ConnectionTester(server).test("/", allow_write_test=True)
    assert all(result.passed for result in results)
    assert server.files == {}


def test_preflight_is_non_destructive_and_finds_site(wordpress_server, tmp_path):
    report = PreflightService(wordpress_server, tmp_path, minimum_free_bytes=1).run("/")
    assert report.passed
    assert report.discovery.wordpress
    assert wordpress_server.download_count == 0


def test_preflight_tries_hosting_control_panel_root_when_login_root_is_empty(tmp_path):
    site = "/domains/safirezaman.com/public_html"
    tree = {
        "/": [],
        site: [file(f"{site}/wp-config.php", "wp-config.php"),
               directory(f"{site}/wp-admin", "wp-admin"),
               directory(f"{site}/wp-content", "wp-content"),
               directory(f"{site}/wp-includes", "wp-includes")],
        f"{site}/wp-admin": [], f"{site}/wp-content": [], f"{site}/wp-includes": [],
    }
    server = FakeServer(tree); server.connect()
    report = PreflightService(server, tmp_path, minimum_free_bytes=1).run("/", (site,))
    assert report.discovery.wordpress
    assert report.discovery.site_root == site


def test_preflight_ignores_permission_denied_for_unrelated_fallback(tmp_path):
    class RestrictedServer(FakeServer):
        def stat(self, path):
            if normalize_remote_path(path) == "/public_html":
                from app.server.errors import PermissionDenied
                raise PermissionDenied("list this remote directory")
            return super().stat(path)

    server = RestrictedServer({"/": []}); server.connect()
    report = PreflightService(server, tmp_path, minimum_free_bytes=1).run("/", ("/public_html",))
    assert any(check.name == "Website discovery" for check in report.checks)
    assert not any(check.name == "Remote access" for check in report.checks)


def test_credentials_are_ephemeral_and_redacted():
    credentials = RuntimeCredentials("alice", "super-secret")
    config = ConnectionConfig(Protocol.SFTP, "example.com", 22)
    assert "super-secret" not in repr(credentials)
    assert "password" not in repr(config).casefold()
    provider = RuntimeCredentialProvider()
    with pytest.raises(NotImplementedError):
        provider.save("example", credentials)
