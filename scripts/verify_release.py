"""Fail a release build when versions or forbidden tracked artifacts are inconsistent."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.version import APP_VERSION  # noqa: E402


def main() -> int:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if metadata["project"]["version"] != APP_VERSION:
        raise SystemExit("pyproject.toml and APP_VERSION differ")
    tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    forbidden = [path for path in tracked if path.startswith("ProjectData/") or
                 Path(path).suffix.casefold() in {".log", ".db", ".sqlite", ".pem", ".key", ".pfx", ".dump"}]
    if forbidden: raise SystemExit("Forbidden release artifacts are tracked: " + ", ".join(forbidden))
    print(f"ImageForge {APP_VERSION} release metadata verified; {len(tracked)} tracked files audited.")
    return 0


if __name__ == "__main__": raise SystemExit(main())
