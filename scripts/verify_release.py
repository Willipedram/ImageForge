"""Verify release metadata, with an optional Git-aware artifact audit."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.version import APP_VERSION  # noqa: E402


def _tracked_files() -> list[str] | None:
    """Return tracked paths only when this source tree is a usable Git checkout."""
    if not (ROOT / ".git").exists():
        return None
    try:
        return subprocess.check_output(
            ["git", "ls-files"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).splitlines()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def main() -> int:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if metadata["project"]["version"] != APP_VERSION:
        raise SystemExit("pyproject.toml and APP_VERSION differ")
    tracked = _tracked_files()
    if tracked is None:
        # End users commonly build from GitHub's source ZIP and need neither
        # Git nor repository metadata. Version consistency is still verified;
        # the tracked-file audit is only meaningful in a Git checkout.
        print(
            f"ImageForge {APP_VERSION} release metadata verified; "
            "Git is unavailable, so the tracked-file audit was skipped."
        )
        return 0
    forbidden = [path for path in tracked if path.startswith("ProjectData/") or
                 Path(path).suffix.casefold() in {".log", ".db", ".sqlite", ".pem", ".key", ".pfx", ".dump"}]
    if forbidden: raise SystemExit("Forbidden release artifacts are tracked: " + ", ".join(forbidden))
    print(f"ImageForge {APP_VERSION} release metadata verified; {len(tracked)} tracked files audited.")
    return 0


if __name__ == "__main__": raise SystemExit(main())
