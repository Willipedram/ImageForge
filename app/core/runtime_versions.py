"""Auditable runtime and encoder capability reporting."""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import json
import platform

from app.core.version import APP_VERSION


def _installed_version(distribution: str, module: str) -> str:
    """Return a useful version even when a freezer omitted dist-info metadata."""
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        loaded_module = importlib.import_module(module)
        return str(getattr(loaded_module, "__version__", "bundled"))


def runtime_versions() -> dict[str, str]:
    versions = {"application": APP_VERSION, "python": platform.python_version()}
    packages = {"pillow": ("Pillow", "PIL"), "pyside6": ("PySide6", "PySide6"),
                "paramiko": ("paramiko", "paramiko"), "pymysql": ("PyMySQL", "pymysql"),
                "psutil": ("psutil", "psutil")}
    for key, (distribution, module) in packages.items():
        versions[key] = (
            _installed_version(distribution, module)
            if importlib.util.find_spec(module)
            else "unavailable"
        )
    if versions["pillow"] != "unavailable":
        features = importlib.import_module("PIL.features")
        versions["webp_encoder"] = (
            "available" if features.check("webp") else "unavailable"
        )
        versions["avif_encoder"] = (
            "available" if features.check("avif") else "unavailable"
        )
    else:
        versions["webp_encoder"] = versions["avif_encoder"] = "unavailable"
    return versions


def runtime_versions_json() -> str:
    return json.dumps(runtime_versions(), sort_keys=True, separators=(",", ":"))
