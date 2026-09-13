# ImageForge

ImageForge 1.0.0 is a native Windows desktop system for safely optimizing website images. It provides durable jobs, remote discovery, image intelligence, auditable decisions, local-folder optimization, and a checkpointed production deployment pipeline.

## Architecture

```text
app/
├── main.py              # bootstrap and desktop entry point
├── config/              # typed, non-secret settings
├── core/                # state machines, engine, retries, versions, errors
├── database/            # SQLite state, MySQL discovery, backups, safe transforms
├── image/               # image analysis, classification, inventory building
├── server/              # FTP/FTPS/SFTP, preflight, discovery, scanning
├── online/              # staged upload, reference update, verification pipeline
├── offline/             # local dry-run, apply, and recovery workflow
├── safety/              # final audit, HTTP checks, rollback, retention
├── storage/             # ProjectData lifecycle and schema control
├── ui/                  # PySide6 window, pages, dashboard, styling
├── utils/               # human/structured logging
└── workers/             # cooperative thread worker contract
tests/                   # persistence, model, logging, startup, and UI tests
ProjectData/             # runtime data; ignored by Git and never deleted by the app
```

The dashboard exposes stable update methods rather than coupling widgets to processing. Future operation executors should work in `QThread`/thread-pool workers, emit bounded progress signals, process file records in pages or streams, and cooperatively check pause/cancellation flags. This keeps the Qt event loop responsive and avoids retaining thousands of image objects.

## Durable job engine

`ProjectData/jobs/state.db` is a WAL-mode SQLite database containing `jobs`, `job_stages`, `job_items`, `events`, `errors`, `checkpoints`, and target locks. `PRAGMA user_version` versions the database independently of the ProjectData directory schema. State changes, audit events, and safe checkpoints commit in one transaction, so an interrupted write cannot leave a partially recorded transition.

Job and item state machines reject invalid transitions. A target lock prevents concurrent jobs from changing the same normalized target while allowing independent targets. Pause stops future scheduling at a safe boundary, cancel records durable state before releasing the target, and terminal jobs retain history counters, sizes, savings, duration, application version, and schema version.

At startup, active jobs become `RECOVERABLE`; the UI lists them and offers Resume, Cancel, Inspect, and Start New Job actions. Recovery reports identify interrupted items and the last safe checkpoint. Interrupted external operations are **not** blindly repeated: a future transport adapter must reconcile the observed local/remote result first. Until a reconciler resolves those items to a safe state, resume is refused. This is the idempotency contract that future FTP/SFTP and deployment phases must implement.

Retries use configurable attempt counts and capped exponential backoff. Errors are stored as `RETRYABLE`, `NON_RETRYABLE`, or `CRITICAL` with their attempt and related item, without storing credentials.

## Remote connectivity and discovery

The protocol-neutral `RemoteServer` interface exposes connect, disconnect, list, stat, download, upload, delete, rename, exists, mkdir, checksum, and bounded-prefix operations. FTP and explicit-TLS FTPS use the Python standard library; SFTP uses Paramiko with system SSH host-key verification enabled by default. SFTP is shown first in the connection UI. Protocol objects do not escape the adapter boundary, and bounded retry/reconnect behavior reuses a single logical connection rather than creating a pool.

Credentials exist only in an in-memory `RuntimeCredentials` object whose password is excluded from representations. The password field is cleared as soon as a background discovery worker is created. Configuration, SQLite state, manifests, and logs never receive credentials. Runtime-only sessions refuse persistence; Windows builds can explicitly store credentials through Windows Credential Manager/DPAPI.

The Server Connection screen tests connectivity, authentication, listing, read access, local disk space, ProjectData, and website discovery on a `QThread`. Its normal preflight is read-only. A separate diagnostic API supports an explicitly requested temporary write/delete permission test and always attempts cleanup; the application UI does not invoke that test in this phase.

Discovery performs configurable, bounded breadth-first traversal instead of assuming `public_html`, `wp-content/uploads`, or a database prefix. It identifies a WordPress root from multiple markers and structural directories, then reports admin, content, includes, uploads, themes, plugins, WooCommerce, and Elementor paths. Permission-denied directories and symlinks are handled safely. The streaming scanner recognizes JPG/JPEG, PNG, GIF, WebP, AVIF, and SVG while preserving Unicode, spaces, special characters, and case-distinct filenames. It obtains size, modification time, MIME type, and—where supported—dimensions from at most 64 KiB of header data without downloading image bodies.

## Image intelligence and WordPress relationships

Full inventory analysis verifies file signatures rather than trusting extensions and records detected format/MIME, dimensions, aspect ratio, color mode, alpha and transparency ratio, semitransparency, animation/frame count, EXIF/orientation, ICC profile, SHA-256, attachment/parent relationships, classification, suspicious content, and skip reasons. Pillow provides pixel-level and decoder validation for raster formats; bounded built-in parsers retain useful PNG, JPEG, GIF, WebP, AVIF, and SVG intelligence when appropriate. Animated WebP/AVIF assets are conservatively skipped, animated GIFs remain animations, and SVG files are parsed as vectors rather than rasterized. SVG document types, entities, scripts, event handlers, foreign objects, and external/JavaScript references are flagged.

The inventory builder processes one downloaded file at a time, deletes its temporary copy, commits bounded batches to the job database, and then maps relationships in pages. It recognizes standard `-WIDTHxHEIGHT` WordPress derivatives only when the matching original exists. Attachment metadata can authoritatively override filename inference. Database preparation discovers the WordPress prefix from the actual `posts`, `postmeta`, and `options` table set and identifies `termmeta` and other relevant tables without assuming `wp_`; it never modifies them.

The Images screen reads persisted inventory in pages and shows totals, bytes, format distribution, transparent and animated assets, derivatives, suspicious files, and skipped files. Filters cover each category and supported format; at most 500 matching rows are materialized by the UI at once.

## Offline optimization engine

`OfflineOptimizer` accepts only local files and performs no network operations. For each file it detects and decodes the real format, applies EXIF orientation, generates format-appropriate candidates, decodes and validates every candidate, compares size and visual fidelity, and selects only the smallest candidate that clears configurable byte and percentage savings thresholds. JPEG candidates use quality, progressive encoding, chroma subsampling, and encoder optimization; PNG candidates use lossless compression; photo-like rasters may receive lossy WebP/AVIF candidates while graphics and alpha assets receive lossless WebP candidates. Existing WebP/AVIF inputs are decoded and genuinely re-encoded rather than renamed.

Validation requires a matching encoded signature, successful decoder verification, identical displayed dimensions, unchanged alpha/transparency/semitransparency, retained ICC data when present, and configurable PSNR for lossy output. EXIF orientation is baked into pixels before the orientation tag is removed. Animated inputs are retained to prevent lost frames; animated WebP/AVIF remain skipped. CMYK, LAB, floating-point, 16-bit, HDR-like, suspicious SVG, oversized, decompression-bomb, corrupt, and otherwise uncertain inputs conservatively retain the original.

Candidates are generated under `ProjectData/jobs/<job-id>/processed/.candidates`; only a selected result is moved into `processed`. Originals are never overwritten. The SQLite schema records original/candidate paths and bytes, actual format, encoder parameters, dimensions, alpha metrics, checksum, validation status, savings, and the decision reason. Safe SVG optimization removes only non-visual `metadata`/`desc` elements, reparses the XML, requires meaningful savings, and never rasterizes the document.

Processing is incremental through `optimize_many`, and `ResourceManager` limits concurrent decoders and per-image pixels. Configurable file-byte, pixel, worker, savings, fidelity, and elapsed-time limits protect local resources. Each result is persisted independently so thousands of files do not need to remain in memory.

## Format selection and quality gate

Format selection is separate from encoding and is never based on size alone. Every WebP or AVIF candidate is assessed for existence, signature, decoding, corruption, dimensions, normalized orientation, compatible color, alpha presence, transparent and semitransparent regions, meaningful savings, quality, compatibility, and processing cost. A smaller AVIF can lose to a slightly larger, visibly safer WebP; neither format receives an automatic preference. If all compatible candidates fail—or confidence is low—the original is retained. Decisions always include a user-readable explanation.

Quality evaluation uses a pluggable `QualityEvaluator` protocol and a default pipeline containing global luminance SSIM, RGB PSNR, and normalized perceptual difference. New metrics can be added without changing the decision engine. The `SAFE` default profile uses the strictest quality and savings thresholds; `BALANCED` and `AGGRESSIVE` remain bounded alternatives. Logos, icons, screenshots, illustrations, text-heavy images, and transparent graphics receive stricter thresholds than photos.

SQLite schema version 4 stores a canonical decision manifest for every persisted result. Each manifest contains the decision-engine version, profile, exact thresholds, original facts, all candidate assessments and metrics, per-candidate verdicts and rejection reasons, selected format, confidence (`HIGH`, `MEDIUM`, or `LOW`), and final explanation. JSON fields use stable key ordering and compact separators so inputs are reproducible and audits can explain precisely why `ORIGINAL`, `WEBP`, `AVIF`, or `SKIP` was chosen.

## Full offline workflow

The **Offline Optimization** page provides the complete local workflow without creating a server adapter or network connection: select a Windows folder, scan, analyze, optimize, quality-validate, compare, preview, explicitly apply, and view a report. Scanning only records file metadata and checksums. A dry run generates candidates under ProjectData, persists every proposal, and leaves source files byte-for-byte unchanged. The preview shows original, WebP, AVIF, and selected representations where Qt supports the codec, along with dimensions, candidate sizes, savings, confidence, and the full decision reason.

Apply is unavailable without explicit confirmation. Each proposed file is rechecked against its scan-time modification time and SHA-256, copied to an immutable per-job backup, and the backup checksum is verified. The selected candidate is copied to a same-directory temporary file, flushed, checksummed, atomically installed with `os.replace`, and then signature/checksum verified. A format-changing result removes the old extension only after the new file passes final verification. Any failure restores the verified backup; the workflow never deletes an original before a valid replacement exists.

SQLite schema version 6 stores local roots, dry-run state, source identity, per-file stages, candidate identity, backup/target paths, errors, and final status. Checkpoints cover scanning, candidate decisions, backup verification, staging, replacement, final verification, and failures. On restart, the newest unfinished offline job is offered in the UI. Recovery reconciles the actual source, temporary, target, candidate, and backup checksums before continuing, and already completed items are never optimized or applied again.

Progress is emitted from a `QThread` with current image, stage, completed/total counts, percentage, elapsed time, ETA, and files per second. Pause stops scheduling after the current safe file, Resume continues from persisted states, and Cancel preserves all state. Reports include totals, optimized/skipped/failed counts, original/final bytes, savings and reduction, selected-format distribution, duration, and errors. Database reads are paged and optimization remains one-file-at-a-time.

## Online website pipeline

The **Online Pipeline** integrates preflight, heuristic WordPress discovery, streaming inventory, verified downloads, local optimization and quality decisions, local backups, staging uploads, remote verification, transactional reference updates, final verification, and deferred cleanup. Image bodies are processed one at a time and state is committed after every safe boundary. Network calls use the configured capped exponential retry policy and reconnect a single server adapter rather than creating connection pools.

Production candidates are uploaded below `optimization-temp/<job-id>/` and checked for existence, size, checksum, signature, and readability. When a server cannot provide checksums, ImageForge streams the staged object back into ProjectData for SHA-256 and signature validation. Only a verified staging object can be atomically renamed into its production sidecar path. Existing, different sidecars cause a safe conflict instead of being overwritten. The original remote image is downloaded to `ProjectData/backups/online/<job-id>/` and checksum-verified before upload, remains present after deployment, and is never deleted in Phase 8.

A `ReferenceUpdater` boundary requires prepare, transactional apply, and verify operations. The production WordPress adapter is injected with runtime credentials; credentials are never represented in the online manifest. SQLite persists original/candidate paths, sizes and checksums, selected format and reason, upload/verification/database states, retries, errors, and stage checkpoints. Restart reconciliation inspects local partial downloads, staging objects, and promoted sidecars before scheduling work again. The Online Pipeline page runs the coordinator on a `QThread` and displays stage, current file, byte/file progress, elapsed time, ETA, retries, errors, and per-item manifest state.

## WordPress database safety

Phase 9 provides a MySQL/MariaDB `WordPressReferenceUpdater` behind the online pipeline's existing transaction boundary. Connection passwords remain runtime-only and are excluded from representations; PyMySQL uses `utf8mb4`, explicit transactions, and a server-side streaming cursor. Table discovery derives the prefix from the actual `posts`, `postmeta`, and `options` set, then inventories every text/JSON column in every prefixed core, plugin, WooCommerce, Elementor, comment, option, term, and metadata table with a usable primary key. No `wp_` prefix is assumed.

Before dry-run inventory, ImageForge streams a complete logical backup of every table in the selected database—not only WordPress-prefixed tables—to `ProjectData/jobs/<job-id>/database/wordpress-full-backup.jsonl.gz`. Binary, decimal, and temporal values are type-tagged; schema statements and rows are retained. A SHA-256 sidecar is generated, the gzip stream and every JSON record are read back, and mutation is refused unless verification succeeds. If a later controlled batch fails after earlier batches committed, the verified full backup is restored and persisted changes are marked `ROLLED_BACK`.

Reference discovery recognizes HTTP/HTTPS URLs, root-relative and uploads-relative paths, URL-encoded and slash-escaped forms, HTML URL attributes and `srcset`, recursive JSON values, and nested PHP arrays. PHP serialized strings are parsed byte-for-byte and reserialized with corrected UTF-8 byte lengths; objects, references, custom serialization, malformed lengths, and invalid JSON are never guessed and are marked `REVIEW`. HTML replacement is limited to URL-bearing attributes so unrelated prose is untouched. Attachment metadata arrays retain dimensions, sizes, and relationships while only matching path strings change.

The dry run is persisted in SQLite schema version 7 with table, primary key, record ID, column, old value, proposed value, change type, status, and review reason. Reference-graph edges connect each image to attachment metadata, content, products, widgets, and plugin rows. The **Database Review** page displays every proposal before mutation. Explicit approval is impossible while any record requires review. Approved writes use optimistic `WHERE primary_key AND old_value` guards in controlled batches, followed by structural validation, confirmation of new values, and a database-wide search for stale old references. Blind SQL `REPLACE` is never used.

## Final safety, cleanup, and rollback

Phase 10 adds a `FinalSafetyCoordinator` after database and remote verification. It first builds an atomic, job-associated backup bundle in `ProjectData/backups/jobs/<job-id>/`. The bundle contains the online image manifest, relevant WordPress reference metadata, the job record, a consistent SQLite job-state snapshot made with SQLite's backup API, and the path and SHA-256 of the already verified complete database backup. Bundle files are checksummed and reread before cleanup can proceed; original image backups remain under the matching `ProjectData/backups/online/<job-id>/` tree.

For every candidate, cleanup requires all gates to pass: remote existence, decoding/signature, MIME, exact dimensions, SHA-256, prior remote verification, database and WordPress metadata verification, absence of old references, verified original backup, verified bundle, committed job checkpoint, and public HTTP verification. The HTTP verifier checks status, MIME, decodability, dimensions, and reports Cloudflare, LiteSpeed, WordPress, proxy, or CDN cache indicators without purging caches. Audit evidence and cleanup authorization are committed before deletion. Any false or unknown gate retains the original. A cleanup transport failure is recorded as a warning because the verified candidate and references remain correct; it does not falsely mark the optimized image as broken.

Cleanup is idempotent after crashes: a restart reconciles committed authorization with actual remote existence and does not repeat completed work. `rollback(job_id)` verifies the bundle, atomically restores any missing originals from their checksummed backups, restores the complete database (including attachment metadata), confirms original references, and only then removes optimized sidecars. Final reports include optimized/skipped/failed counts, deleted and retained originals, updated database records, verification errors, warnings, realized savings, and duration.

Backup retention is explicit and supports 7 days, 30 days, 90 days, or never. `never` performs no deletion. The retention service only removes expired backups for terminal jobs when directly invoked with a finite policy; it refuses symlinks and paths outside ProjectData. Source replacement never deletes ProjectData automatically.

## Professional dashboard and live monitoring

Phase 11 reorganizes the native interface into **Dashboard, Servers, Scan, Images, Jobs, Database, Backups, Recovery, Logs, and Settings**. Offline and online workflows share the Scan workspace, while operational history, recovery, database review, and backup state remain focused pages. Tables use hard result limits or paged repositories; image rows show the original and selected representation, format, original/final sizes, savings, decision, confidence, and status without eagerly decoding thumbnail collections. Jobs show ID, target, date, status, files, savings, duration, and application version, with view, resume, cancel, rollback-request, and JSON export actions.

The dashboard receives immutable snapshots from a dedicated `QThread`; the Qt main thread performs no system sampling or unbounded job query. It displays current job, overall progress, processed/remaining files, original/final sizes, savings and reduction, current stage, elapsed time, remaining time, clock ETA, CPU, RAM, disk, bidirectional network throughput, active/allocated workers, and individual estimates for Scan, Download, Optimize, Upload, Database, Verification, and Cleanup. ETA remains **Calculating ETA...** until measured or historical rates exist. The estimator uses recent weighted samples, bytes, pixels, format, workers, current stage, and completed-stage history rather than fixed durations.

Resource profiles are **ECO, BALANCED, PERFORMANCE, and CUSTOM**, with BALANCED as the default. A shared adaptive manager owns independent download, optimization, and upload scheduling gates plus a global server-connection gate. High RAM or extreme CPU pressure immediately reduces future scheduling; available CPU/RAM/network capacity increases one worker at a time within configured limits. Active work is never killed when a limit shrinks—it finishes at the next safe boundary—so responsiveness and checkpoint guarantees are preserved.

The live Logs page shows a bounded event stream using INFO, OK, WARNING, ERROR, CRITICAL, SKIPPED, and RESUMED labels and trims old rendered blocks. Light and dark themes use a consistent Segoe UI visual system, compact cards, restrained color, and immediate theme switching. Resource sampling is provided by `psutil`; all production dependencies are installed with the standard package command below.

## Installation and running

Windows users who only want to run the application can download the `ImageOptimizer-Windows-x64` artifact from the latest successful **Windows executable** GitHub Actions run, extract it, and double-click `ImageOptimizer.exe`. Python is not required for the packaged application. See [Installation and Windows executable build](INSTALL.md) for download, checksum, SmartScreen, and local-build instructions.

Python 3.11 or newer is recommended.

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m app.main
```

To verify bootstrap without opening a window:

```powershell
python -m app.main --check-startup
```

To select an existing durable data directory:

```powershell
python -m app.main --project-data "D:\ImageForgeData"
$env:IMAGEFORGE_PROJECT_DATA = "D:\ImageForgeData"  # alternative
```

The command-line option takes precedence over the `IMAGEFORGE_PROJECT_DATA` environment variable. The Settings page manages non-secret preferences. Changing the data directory is intentionally a launch-time operation so an in-use database is never moved underneath active workers.

## ProjectData and upgrades

By default, `ProjectData` is beside the source/application directory. It contains `jobs`, `backups`, `logs`, `cache`, `config`, `database`, and `manifests`. Source replacement never removes it, so a new ImageForge version can point at the same directory. `data_schema.json` records `data_schema_version = 2` (in JSON form), and startup refuses data created by a newer unsupported application.

The schema manager is designed to require an explicit migration for each version. Before a future migration it copies durable content to a timestamped backup, applies migrations incrementally, verifies the final version, and logs completion. Logs rotate in `ProjectData/logs/imageforge.log`; SQLite job state lives in `ProjectData/jobs/state.db`.

Configuration contains only UI and operational preferences: language, theme, optimization profile, worker and retry limits, capped retry delays, retention, and logging level. Passwords, tokens, private keys, and database credentials are never written there. Runtime-only sessions remain available; packaged Windows builds can use Windows Credential Manager/DPAPI.

## Release documentation

- [Installation and Windows executable build](INSTALL.md)
- [Architecture and recovery model](ARCHITECTURE.md)
- [Development and release process](DEVELOPMENT.md)
- [Security policy](SECURITY.md)
- [Release history](CHANGELOG.md)

## Development

```bash
python -m pip install -e '.[dev]'
pytest
QT_QPA_PLATFORM=offscreen python -m app.main --check-startup --project-data /tmp/imageforge-check
```

Keep UI work on the Qt main thread and all expensive or blocking work in workers. Job records should contain resumable metadata; large file collections and image bytes must be streamed or paged rather than loaded into memory.

## Roadmap

Version 1.0.0 completes the production foundation, QA, packaging, monitoring, guarded cleanup, and job-ID rollback. Provider-specific cache purge integrations and raster-to-vector conversion remain intentionally out of scope.
