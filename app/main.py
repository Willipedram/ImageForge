"""ImageForge command-line and desktop entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

from app.config.settings import AppSettings, ConfigurationStore, default_project_data_path
from app.core.errors import ErrorHandler
from app.core.engine import JobEngine
from app.core.retry import RetryPolicy
from app.database.jobs import JobRepository
from app.storage.project_data import ProjectDataManager
from app.utils.logging import configure_logging


def bootstrap(project_data: Path | None = None) -> tuple[AppSettings, ConfigurationStore]:
    """Initialize persistence, settings, and logs independently of Qt."""
    requested = (project_data or default_project_data_path()).expanduser().resolve()
    manager = ProjectDataManager(requested)
    manager.initialize()
    JobRepository(requested / "jobs" / "state.db").initialize()
    store = ConfigurationStore(requested)
    settings = store.load()
    # The explicit/environment path is authoritative for this launch.
    settings.project_data_path = str(requested)
    if not store.path.exists():
        store.save(settings)
    configure_logging(requested, settings.logging_level)
    logging.getLogger(__name__).info("ImageForge initialized with ProjectData at %s", requested)
    return settings, store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ImageForge desktop application")
    parser.add_argument("--project-data", type=Path, help="Persistent ProjectData directory")
    parser.add_argument("--check-startup", action="store_true", help="Initialize services without opening a window")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings, store = bootstrap(args.project_data)
    if args.check_startup:
        return 0
    from PySide6.QtWidgets import QApplication
    from app.ui.main_window import MainWindow
    from app.ui.theme import STYLESHEET

    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    application = QApplication(sys.argv if argv is None else [sys.argv[0], *argv])
    application.setApplicationName("ImageForge")
    application.setOrganizationName("ImageForge")
    application.setStyleSheet(STYLESHEET)
    repository = JobRepository(Path(settings.project_data_path) / "jobs" / "state.db")
    repository.initialize()
    engine = JobEngine(repository, RetryPolicy(
        max_attempts=settings.retry_count,
        base_delay_seconds=settings.retry_base_delay_seconds,
        maximum_delay_seconds=settings.retry_maximum_delay_seconds,
    ))
    engine.recover_interrupted_jobs()
    window = MainWindow(settings, store, engine)
    ErrorHandler(window.show_error).install()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
