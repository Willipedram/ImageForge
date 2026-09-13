# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/) conventions. ImageForge uses semantic versioning.

## [1.0.0] — 2026-09-13

### Added

- Native PySide6 Windows application with dashboard, server, scan, image, job, database, backup, recovery, log, and settings workspaces.
- Durable ProjectData, SQLite migrations, target locks, checkpoints, recovery, pause/resume/cancel, retries, and audit events.
- FTP, explicit FTPS, and SFTP adapters; bounded WordPress discovery and image scanning.
- Signature-first image intelligence and conservative offline WebP/AVIF/SVG optimization with explainable quality decisions.
- Local dry-run/apply and staged online workflows with verified backups and atomic replacement boundaries.
- Structure-aware WordPress MySQL/MariaDB reference updates, PHP serialization, JSON/HTML handling, full logical backup, verification, and rollback.
- Final cleanup gates, HTTP/cache awareness, job backup bundles, explicit retention policies, and Job-ID rollback.
- Adaptive resource pools, live monitoring, measured/historical ETA, light/dark themes, and production packaging configuration.

### Security

- Runtime-only credentials and Windows Credential Manager integration; no plaintext secret persistence.
- Remote-path traversal rejection, parameterized SQL values, validated SQL identifiers, guarded XML parsing, resource limits, and checksum verification.

