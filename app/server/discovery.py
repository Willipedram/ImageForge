"""Bounded heuristic WordPress discovery and metadata-only image scanning."""

from __future__ import annotations

import mimetypes
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.image.metadata import dimensions_from_header
from app.server.base import RemoteServer
from app.server.errors import PermissionDenied
from app.server.paths import normalize_remote_path, safe_join

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg"})


@dataclass(frozen=True, slots=True)
class DiscoveryOptions:
    max_depth: int = 6
    max_directories: int = 10_000
    follow_symlinks: bool = False
    root_markers: tuple[str, ...] = ("wp-config.php", "wp-load.php", "index.php")
    content_names: tuple[str, ...] = ("wp-content", "content", "app")
    uploads_names: tuple[str, ...] = ("uploads", "media")
    themes_names: tuple[str, ...] = ("themes",)
    plugins_names: tuple[str, ...] = ("plugins",)
    max_trace_directories: int = 250
    ignored_directory_names: tuple[str, ...] = (
        ".htpasswd", "logs", "stats", "public_ftp", "backups", "mail", "tmp",
    )


@dataclass(frozen=True, slots=True)
class DiscoveryTrace:
    path: str
    depth: int
    status: str
    entry_count: int = 0
    child_directories: tuple[str, ...] = ()
    found_markers: tuple[str, ...] = ()
    missing_markers: tuple[str, ...] = ()
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class SiteDiscovery:
    search_root: str
    site_root: str | None
    wordpress: bool
    wp_admin: str | None = None
    wp_content: str | None = None
    wp_includes: str | None = None
    uploads: str | None = None
    themes: str | None = None
    plugins: str | None = None
    woocommerce: bool = False
    elementor: bool = False
    closest_path: str | None = None
    missing_markers: tuple[str, ...] = ()
    directories_checked: int = 0
    empty_directories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RemoteImage:
    path: str
    extension: str
    size: int | None
    modified_at: object | None
    mime_type: str | None
    width: int | None = None
    height: int | None = None


class SiteDiscoverer:
    def __init__(self, server: RemoteServer, options: DiscoveryOptions | None = None,
                 trace: Callable[[DiscoveryTrace], None] | None = None) -> None:
        self.server = server
        self.options = options or DiscoveryOptions()
        self.trace = trace
        self._trace_count = 0

    def discover(self, search_root: str = "/") -> SiteDiscovery:
        root = normalize_remote_path(search_root)
        queue = deque([(root, 0)])
        visited: set[str] = set()
        best_path, best_score = root, -1
        best_missing: tuple[str, ...] = ()
        empty_directories: list[str] = []
        while queue and len(visited) < self.options.max_directories:
            directory, depth = queue.popleft()
            key = directory
            if key in visited:
                continue
            visited.add(key)
            try:
                entries = self.server.list(directory)
            except (PermissionError, PermissionDenied) as exc:
                self._emit_trace(DiscoveryTrace(directory, depth, "denied", error_type=type(exc).__name__))
                continue
            names = {entry.name.casefold(): entry for entry in entries}
            if not entries and len(empty_directories) < 50:
                empty_directories.append(directory)
            content_entry = next(
                (names[name.casefold()] for name in self.options.content_names if name.casefold() in names), None
            )
            has_root_marker = any(marker.casefold() in names for marker in self.options.root_markers)
            missing = tuple(
                label for present, label in (
                    ("wp-admin" in names, "wp-admin"),
                    ("wp-includes" in names, "wp-includes"),
                    (content_entry is not None, "wp-content"),
                    (has_root_marker, "wp-config.php/wp-load.php/index.php"),
                ) if not present
            )
            score = 4 - len(missing)
            ignored = {name.casefold() for name in self.options.ignored_directory_names}
            children = tuple(sorted(
                entry.name for entry in entries
                if entry.is_directory and entry.name.casefold() not in ignored
                and (self.options.follow_symlinks or not entry.is_symlink)
            ))
            found = tuple(label for present, label in (
                ("wp-admin" in names, "wp-admin"),
                ("wp-includes" in names, "wp-includes"),
                (content_entry is not None, content_entry.name if content_entry else "wp-content"),
                (has_root_marker, next(
                    (marker for marker in self.options.root_markers if marker.casefold() in names),
                    "root PHP marker",
                )),
            ) if present)
            self._emit_trace(DiscoveryTrace(
                directory, depth, "wordpress_found" if not missing else "inspected",
                len(entries), children, found, missing,
            ))
            if score > best_score:
                best_path, best_score, best_missing = directory, score, missing
            wordpress = not missing
            if wordpress:
                content = content_entry.path
                uploads = self._first_existing(content, self.options.uploads_names)
                themes = self._first_existing(content, self.options.themes_names)
                plugins = self._first_existing(content, self.options.plugins_names)
                return SiteDiscovery(
                    root, directory, True,
                    names["wp-admin"].path, content, names["wp-includes"].path,
                    uploads, themes, plugins,
                    bool(plugins and self.server.exists(safe_join(plugins, "woocommerce"))),
                    bool(plugins and self.server.exists(safe_join(plugins, "elementor"))),
                    directory, (), len(visited), tuple(empty_directories),
                )
            if depth < self.options.max_depth:
                for entry in entries:
                    if entry.is_directory and entry.name.casefold() in ignored:
                        self._emit_trace(DiscoveryTrace(
                            entry.path, depth + 1, "skipped_non_web_directory"
                        ))
                children = sorted(
                    (entry for entry in entries if entry.is_directory
                     and entry.name.casefold() not in ignored
                     and (self.options.follow_symlinks or not entry.is_symlink)),
                    key=lambda entry: self._priority(entry.name),
                )
                queue.extend((entry.path, depth + 1) for entry in children)
        return SiteDiscovery(
            root, None, False, missing_markers=best_missing,
            closest_path=best_path, directories_checked=len(visited),
            empty_directories=tuple(empty_directories),
        )

    def _emit_trace(self, trace: DiscoveryTrace) -> None:
        if self.trace is None or self._trace_count >= self.options.max_trace_directories:
            return
        self._trace_count += 1
        try:
            self.trace(trace)
        except Exception:
            # Diagnostics must never interrupt read-only discovery.
            return

    def _first_existing(self, parent: str, names: tuple[str, ...]) -> str | None:
        for name in names:
            path = safe_join(parent, name)
            if self.server.exists(path):
                return path
        return None

    @staticmethod
    def _priority(name: str) -> tuple[int, str]:
        common = {"public_html", "www", "htdocs", "httpdocs", "web", "site"}
        return (0 if name.casefold() in common else 1, name.casefold())


class RemoteImageScanner:
    """Yields metadata lazily and never downloads image bodies."""

    def __init__(self, server: RemoteServer, *, follow_symlinks: bool = False,
                 max_directories: int = 100_000, inspect_dimensions: bool = True) -> None:
        self.server = server
        self.follow_symlinks = follow_symlinks
        self.max_directories = max_directories
        self.inspect_dimensions = inspect_dimensions

    def scan(self, root: str):
        queue = deque([normalize_remote_path(root)])
        visited: set[str] = set()
        while queue and len(visited) < self.max_directories:
            directory = queue.popleft()
            key = directory
            if key in visited:
                continue
            visited.add(key)
            try:
                entries = self.server.list(directory)
            except (PermissionError, PermissionDenied):
                continue
            for entry in entries:
                if entry.is_symlink and not self.follow_symlinks:
                    continue
                if entry.is_directory:
                    queue.append(entry.path)
                    continue
                extension = PurePosixPath(entry.name).suffix.casefold()
                if extension in IMAGE_EXTENSIONS:
                    dimensions = None
                    if self.inspect_dimensions and extension in {".jpg", ".jpeg", ".png", ".gif"}:
                        try:
                            dimensions = dimensions_from_header(self.server.read_prefix(entry.path, 64 * 1024))
                        except (OSError, PermissionError):
                            dimensions = None
                    yield RemoteImage(
                        entry.path, extension, entry.size, entry.modified_at,
                        entry.mime_type or mimetypes.guess_type(entry.name)[0],
                        dimensions[0] if dimensions else None, dimensions[1] if dimensions else None,
                    )
