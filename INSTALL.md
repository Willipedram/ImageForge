# Installation and Windows Release Build

## Supported environment

- Windows 10 or Windows 11, 64-bit
- Python 3.11 or newer for source installations
- Sufficient local storage for downloaded originals, candidates, and verified backups

## Run from source

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m app.main
```

Use an existing external data directory with:

```powershell
python -m app.main --project-data "D:\ImageForgeData"
```

## Build the executable

From a clean Windows checkout:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\build_windows.ps1
```

The script creates an isolated build environment, installs production and test dependencies plus PyInstaller and pywin32, runs all tests and release checks, then produces `dist\ImageOptimizer.exe`.

For a build without the command line, install Python 3.11 or newer (enabling **Add python.exe to PATH**) and double-click `build_windows.bat` in the project folder. The build script supports both the optional `py.exe` launcher and `python.exe`, performs the same verified build, and selects the finished executable in File Explorer. If neither command is available, it stops with installation instructions instead of an unexplained command-not-found error.

## Download the ready-to-run executable

No Python installation is required on the end user's computer. In GitHub, open **Actions**, select **Windows executable**, choose the latest successful run, and download the `ImageOptimizer-Windows-x64` artifact. Extract the downloaded ZIP, then double-click `ImageOptimizer.exe`.

Release maintainers can create the same downloadable artifact by running the workflow manually or by pushing a version tag such as `v1.0.0`. The artifact also includes `ImageOptimizer.exe.sha256`, which can be used to verify that the download was not corrupted.

Windows may show a SmartScreen warning for an unsigned build. Verify that the artifact came from this repository and compare its SHA-256 checksum before choosing to run it. Production distributors should code-sign the executable with their trusted Windows signing certificate.

## Portable deployment

Copy only the executable to a writable folder:

```text
ImageOptimizer\
├── ImageOptimizer.exe
└── ProjectData\       # created on first launch; never embedded in the executable
```

When frozen, ImageForge defaults to `ProjectData` beside the executable. Keep that directory when replacing the executable with a newer version. Alternatively, launch with `--project-data` or set `IMAGEFORGE_PROJECT_DATA` to use a fixed durable location.

## Troubleshooting

- **UI does not launch:** reinstall `requirements.txt` and update the display driver.
- **SFTP host-key error:** add the correct host key to the Windows user SSH known-hosts file; never disable verification merely to bypass an unexpected key.
- **FTPS certificate error:** correct the server certificate chain. Plain FTP should be used only when the hosting environment offers no secure protocol.
- **Database review blocked:** inspect every `REVIEW` row; unsupported PHP objects are intentionally not changed automatically.
- **Job is recoverable:** open Recovery, inspect the last checkpoint, reconnect using runtime credentials, and resume.
- **Original retained:** inspect Final Safety verification errors. Retention is the expected outcome for any uncertain gate.
