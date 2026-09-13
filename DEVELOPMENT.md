# Development Guide

## Setup

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
pytest -q
python -m compileall -q app tests
python scripts/verify_release.py
```

For headless Qt tests, set `QT_QPA_PLATFORM=offscreen`. Tests use fake servers and an injected DB-API implementation; they never require production credentials or internet access.

## Engineering rules

- Keep image, filesystem, network, database, monitoring, and large-query work off the Qt main thread.
- Stream or page inventories; never retain thousands of decoded images.
- Persist state and a safe checkpoint before external side effects.
- Reconcile observed state after interruption instead of blindly repeating an operation.
- Never log or serialize credentials. Add secrets only through runtime providers or Windows Credential Manager.
- Use `pathlib` locally, normalized POSIX paths remotely, parameterized SQL values, and validated identifiers.
- Preserve original files whenever any integrity, quality, reference, backup, or verification result is unknown.

## Test organization

- `test_foundation.py`: ProjectData, configuration, logging, startup
- `test_job_engine.py`: state transitions, locking, checkpoints, recovery, retry
- `test_server_discovery.py`: protocol boundary, discovery, path safety, resilience
- `test_image_intelligence.py` and `test_optimizer_*`: formats, metadata, limits, encoding, quality
- `test_offline_workflow.py` and `test_online_workflow.py`: resumable workflows and failure safety
- `test_wordpress_database.py`: database inventory, structure-aware replacement, backup and rollback
- `test_final_safety.py`: final deletion gates, HTTP checks, cleanup, rollback, retention
- `test_monitoring.py`: ETA, telemetry, profiles, adaptive worker limits
- `test_release_qa.py`: security, migration, packaging, scale, and release acceptance

## Release process

1. Update `APP_VERSION`, `pyproject.toml`, and `CHANGELOG.md` together.
2. Run the full test and release-verification commands.
3. Build on clean Windows using `scripts/build_windows.ps1`.
4. Smoke-test `dist\ImageOptimizer.exe --check-startup` and the UI in both themes.
5. Sign the executable with the organization’s Windows code-signing certificate outside the repository.
6. Publish the executable and checksums; never publish ProjectData.

