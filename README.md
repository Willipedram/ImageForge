# ImageForge

ImageForge is a native Windows desktop system for safely optimizing website images. Phases 1–5 provide the application shell, durable jobs, secure remote discovery, persistent image intelligence, and an offline candidate optimization engine. It does not yet modify WordPress or remote databases or replace/delete production files.

## Architecture

```text
app/
├── main.py              # bootstrap and desktop entry point
├── config/              # typed, non-secret settings
├── core/                # state machines, engine, retries, versions, errors
├── database/            # transactional SQLite state repository
├── image/               # image analysis, classification, inventory building
├── server/              # FTP/FTPS/SFTP, preflight, discovery, scanning
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

Credentials exist only in an in-memory `RuntimeCredentials` object whose password is excluded from representations. The password field is cleared as soon as a background discovery worker is created. Configuration, SQLite state, manifests, and logs never receive credentials. A credential-provider contract exists for a future Windows Credential Manager/DPAPI implementation; its current runtime provider refuses persistence.

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

## Installation and running

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

By default, `ProjectData` is beside the source/application directory. It contains `jobs`, `backups`, `logs`, `cache`, `config`, `database`, and `manifests`. Source replacement never removes it, so a new ImageForge version can point at the same directory. `data_schema.json` records `data_schema_version = 1` (in JSON form), and startup refuses data created by a newer unsupported application.

The schema manager is designed to require an explicit migration for each version. Before a future migration it copies durable content to a timestamped backup, applies migrations incrementally, verifies the final version, and logs completion. Logs rotate in `ProjectData/logs/imageforge.log`; SQLite job state lives in `ProjectData/jobs/state.db`.

Configuration contains only UI and operational preferences: language, theme, optimization profile, worker and retry limits, capped retry delays, retention, and logging level. Passwords, tokens, private keys, and database credentials are unsupported. A later Windows Credential Manager integration will own secrets.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
QT_QPA_PLATFORM=offscreen python -m app.main --check-startup --project-data /tmp/imageforge-check
```

Keep UI work on the Qt main thread and all expensive or blocking work in workers. Job records should contain resumable metadata; large file collections and image bytes must be streamed or paged rather than loaded into memory.

## Roadmap

Future phases will add upload planning, WordPress reference updates, production verification, rollback, and only then guarded original cleanup. Phase 5 produces local candidates only; remote database changes, production uploads, raster-to-vector conversion, original replacement, and deletion remain out of scope.
