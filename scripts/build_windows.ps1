$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not $IsWindows -and $PSVersionTable.PSEdition -eq "Core") {
    throw "ImageOptimizer.exe must be built on Windows. Use build_windows.bat or the Windows executable GitHub Actions workflow."
}

if (-not (Test-Path ".venv-build")) { py -3.11 -m venv .venv-build }
& .\.venv-build\Scripts\python.exe -m pip install --upgrade pip
& .\.venv-build\Scripts\python.exe -m pip install -r requirements-dev.txt "pywin32>=308"
& .\.venv-build\Scripts\python.exe -m pytest -q
& .\.venv-build\Scripts\python.exe scripts\verify_release.py
& .\.venv-build\Scripts\pyinstaller.exe --noconfirm --clean ImageForge.spec

$Executable = Resolve-Path "dist\ImageOptimizer.exe"
$SmokeData = Join-Path $env:TEMP ("ImageForge-smoke-" + [guid]::NewGuid().ToString("N"))
try {
    & $Executable --check-startup --project-data $SmokeData
    if ($LASTEXITCODE -ne 0) { throw "Packaged executable startup check failed with exit code $LASTEXITCODE" }
} finally {
    if (Test-Path $SmokeData) { Remove-Item -Recurse -Force $SmokeData }
}

Write-Host "Built dist\ImageOptimizer.exe"
Write-Host "The packaged executable passed its startup check and can be opened by double-clicking it."
Write-Host "ProjectData is intentionally not packaged; it will be created beside the executable."
