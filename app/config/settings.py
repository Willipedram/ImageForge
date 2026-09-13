"""Typed, non-secret application settings."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


ENV_PROJECT_DATA = "IMAGEFORGE_PROJECT_DATA"


def default_project_data_path() -> Path:
    """Return the data directory without creating it.

    An environment override makes side-by-side application upgrades able to reuse
    one durable data directory.
    """
    override = os.environ.get(ENV_PROJECT_DATA)
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "ProjectData"


@dataclass(slots=True)
class AppSettings:
    """Safe preferences only; secrets are deliberately unsupported."""

    project_data_path: str
    ui_language: str = "en"
    theme: str = "system"
    default_optimization_profile: str = "balanced"
    max_workers: int = 4
    retry_count: int = 3
    retry_base_delay_seconds: float = 1.0
    retry_maximum_delay_seconds: float = 60.0
    optimization_max_pixels: int = 80_000_000
    optimization_max_file_mb: int = 512
    optimization_timeout_seconds: float = 120.0
    minimum_savings_percent: float = 5.0
    backup_retention_days: int = 30
    logging_level: str = "INFO"

    @classmethod
    def defaults(cls, project_data: Path | None = None) -> "AppSettings":
        return cls(project_data_path=str(project_data or default_project_data_path()))

    def validate(self) -> None:
        if self.max_workers < 1 or self.max_workers > 64:
            raise ValueError("max_workers must be between 1 and 64")
        if self.retry_count < 1 or self.retry_count > 20:
            raise ValueError("retry_count must be between 1 and 20")
        if self.retry_base_delay_seconds < 0 or self.retry_maximum_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")
        if min(self.optimization_max_pixels, self.optimization_max_file_mb,
               self.optimization_timeout_seconds) <= 0:
            raise ValueError("optimization resource limits must be positive")
        if not 0 <= self.minimum_savings_percent < 100:
            raise ValueError("minimum_savings_percent must be between 0 and 100")
        if self.backup_retention_days < 1:
            raise ValueError("backup_retention_days must be positive")
        if self.logging_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("logging_level is invalid")
        if self.theme not in {"system", "light", "dark"}:
            raise ValueError("theme is invalid")


class ConfigurationStore:
    """Atomically persists validated preferences within ProjectData/config."""

    FILE_NAME = "settings.json"

    def __init__(self, project_data: Path) -> None:
        self.project_data = project_data.resolve()
        self.path = self.project_data / "config" / self.FILE_NAME

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings.defaults(self.project_data)
        raw: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        allowed = {field.name for field in fields(AppSettings)}
        settings = AppSettings(**{key: value for key, value in raw.items() if key in allowed})
        settings.validate()
        return settings

    def save(self, settings: AppSettings) -> None:
        settings.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(settings), indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
