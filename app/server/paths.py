"""Remote POSIX path normalization and traversal protection."""

from __future__ import annotations

import posixpath


class UnsafeRemotePath(ValueError):
    pass


def normalize_remote_path(path: str) -> str:
    if "\x00" in path:
        raise UnsafeRemotePath("Remote path contains a null character.")
    portable = path.replace("\\", "/")
    if ".." in portable.split("/"):
        raise UnsafeRemotePath("Remote path contains a parent traversal segment.")
    normalized = posixpath.normpath("/" + portable.lstrip("/"))
    if normalized == "/.." or normalized.startswith("/../"):
        raise UnsafeRemotePath("Remote path escapes its root.")
    return normalized


def safe_join(root: str, *parts: str) -> str:
    base = normalize_remote_path(root)
    candidate = normalize_remote_path(posixpath.join(base, *parts))
    if candidate != base and not candidate.startswith(base.rstrip("/") + "/"):
        raise UnsafeRemotePath("Remote path escapes the configured root.")
    return candidate
