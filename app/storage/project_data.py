"""Durable ProjectData directory lifecycle and schema management."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable

from app.core.version import DATA_SCHEMA_VERSION

DIRECTORIES = ("jobs", "backups", "logs", "cache", "config", "database", "manifests")
SCHEMA_FILE = "data_schema.json"


class ProjectDataError(RuntimeError):
    """Raised when persistent data cannot be safely initialized."""


class ProjectDataManager:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.schema_path = self.root / SCHEMA_FILE

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in DIRECTORIES:
            (self.root / directory).mkdir(exist_ok=True)
        if not self.schema_path.exists():
            self._write_schema(DATA_SCHEMA_VERSION)
            return
        current = self.schema_version()
        if current > DATA_SCHEMA_VERSION:
            raise ProjectDataError(
                f"ProjectData schema {current} is newer than supported schema {DATA_SCHEMA_VERSION}."
            )
        if current < DATA_SCHEMA_VERSION:
            self._migrate(current)

    def schema_version(self) -> int:
        try:
            payload = json.loads(self.schema_path.read_text(encoding="utf-8"))
            return int(payload["data_schema_version"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ProjectDataError("ProjectData schema metadata is invalid.") from exc

    def _write_schema(self, version: int) -> None:
        payload = {
            "data_schema_version": version,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        temporary = self.schema_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.schema_path)

    def _migrate(self, source_version: int) -> None:
        """Safely execute registered migrations when future schemas are added."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = self.root / "backups" / f"schema-v{source_version}-{stamp}"
        backup.mkdir(parents=True, exist_ok=False)
        for item in self.root.iterdir():
            if item.name in {"backups", "logs", "cache"}:
                continue
            destination = backup / item.name
            shutil.copytree(item, destination) if item.is_dir() else shutil.copy2(item, destination)
        logging.getLogger(__name__).info("Created pre-migration backup at %s", backup)

        version = source_version
        migrations: dict[int, Callable[[Path], None]] = {1: self._migrate_v1_to_v2}
        while version < DATA_SCHEMA_VERSION:
            migration = migrations.get(version)
            if migration is None:
                raise ProjectDataError(f"No safe migration exists from schema {version}.")
            migration(self.root)
            version += 1
            self._write_schema(version)
        if self.schema_version() != DATA_SCHEMA_VERSION:
            raise ProjectDataError("ProjectData migration verification failed.")
        logging.getLogger(__name__).info("Migrated ProjectData schema to %d", version)

    @staticmethod
    def _migrate_v1_to_v2(root: Path) -> None:
        """Add durable release metadata without moving or deleting user content."""
        policy = root / "config" / "data_policy.json"
        if not policy.exists():
            temporary = policy.with_suffix(".tmp")
            temporary.write_text(json.dumps({"format": 1, "project_data_external": True,
                "automatic_source_cleanup": False}, indent=2) + "\n", encoding="utf-8")
            temporary.replace(policy)
