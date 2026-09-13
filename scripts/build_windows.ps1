$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path ".venv-build")) { py -3.11 -m venv .venv-build }
& .\.venv-build\Scripts\python.exe -m pip install --upgrade pip
& .\.venv-build\Scripts\python.exe -m pip install -r requirements-dev.txt "pywin32>=308"
& .\.venv-build\Scripts\python.exe -m pytest -q
& .\.venv-build\Scripts\python.exe scripts\verify_release.py
& .\.venv-build\Scripts\pyinstaller.exe --noconfirm --clean ImageForge.spec

Write-Host "Built dist\ImageOptimizer.exe"
Write-Host "ProjectData is intentionally not packaged; it will be created beside the executable."
