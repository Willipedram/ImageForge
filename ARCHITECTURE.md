# Architecture

## Boundaries

| Package | Responsibility |
|---|---|
| `app.core` | Job state machine, retry, ETA, resources, monitoring, versions |
| `app.database` | SQLite repositories, MySQL/MariaDB discovery and safe reference updates |
| `app.server` | Protocol-neutral remote interface and FTP/FTPS/SFTP adapters |
| `app.image` | Signature-first analysis, quality metrics, decisions, candidate encoding |
| `app.offline` | Local dry-run, preview, atomic apply, and resume |
| `app.online` | Discovery-to-staged-production orchestration |
| `app.safety` | Backup bundles, HTTP/final audit, cleanup, retention, rollback |
| `app.storage` | External ProjectData creation and versioned migration |
| `app.ui` | Native Qt presentation and background worker wiring |

## Durable state

ProjectData is external to source and packaged resources. `data_schema.json` versions its directory contract; `jobs/state.db` independently versions SQLite. WAL transactions combine state, event, and checkpoint updates. Every Job records application/data-schema versions, and `job_runtime_versions` records Python, dependency, and WebP/AVIF encoder availability.

```text
ProjectData/
├── jobs/<job-id>/       # downloads, candidates, DB backup, verification
├── backups/             # original images, migration copies, job bundles
├── logs/
├── cache/
├── config/
├── database/
└── manifests/
```

Source upgrades point at the same directory. A ProjectData migration first creates a timestamped backup, applies one registered migration at a time, verifies the resulting schema, and logs completion. SQLite migrations preserve jobs and audit history.

## Safety and recovery

Operations transition through explicit states and reject illegal transitions. Target locks prevent concurrent changes to one site. Pause and cancellation stop at safe boundaries. Startup marks interrupted active jobs recoverable. Reconcilers inspect local checksums, staging objects, production sidecars, and database state before resume.

Remote originals can be removed only after the final safety conjunction succeeds: validated candidate, remote integrity, dimensions/MIME/checksum, updated and verified references/metadata, no old references, verified original and database backups, committed audit state, and public HTTP validation. Rollback restores originals and database references before removing candidates.

## Concurrency and memory

The UI receives immutable snapshots from workers. Adaptive gates isolate download, optimization, and upload concurrency while a shared gate caps total workers and remote connections. Inventories, manifests, backups, logs, and database scans are streamed or paged. Only one image is decoded per optimization reservation.

## Extensibility

Remote protocols implement `RemoteServer`; quality metrics implement `QualityEvaluator`; database mutations implement `ReferenceUpdater`; monitoring uses `MetricsProvider`. These boundaries permit new protocols, metrics, credential stores, and database engines without coupling them to workflows or UI widgets.

