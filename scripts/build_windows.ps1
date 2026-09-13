[CmdletBinding()]
param(
    [switch]$RunTests
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if ($env:OS -ne "Windows_NT") {
    throw "ImageOptimizer.exe must be built on Windows. Use build_windows.bat or the Windows executable GitHub Actions workflow."
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command $($Arguments -join ' ')"
    }
}

# The Python launcher (py.exe) is optional. Fall back to python.exe because
# installations from python.org, winget, Chocolatey, and CI expose Python in
# different ways.
$BootstrapPython = $null
$BootstrapArguments = @()
if (Get-Command "py.exe" -ErrorAction SilentlyContinue) {
    & py.exe -3.11 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
    if ($LASTEXITCODE -eq 0) {
        $BootstrapPython = "py.exe"
        $BootstrapArguments = @("-3.11")
    }
}
if (-not $BootstrapPython -and (Get-Command "python.exe" -ErrorAction SilentlyContinue)) {
    & python.exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
    if ($LASTEXITCODE -eq 0) {
        $BootstrapPython = "python.exe"
    }
}

if (-not $BootstrapPython) {
    throw @"
Python 3.11 or newer was not found (an older version or a Microsoft Store
placeholder does not count).
Install Python from https://www.python.org/downloads/windows/ and enable
'Add python.exe to PATH', then close this window and run build_windows.bat again.
If you only want to use ImageForge, download the ready-made
ImageOptimizer-Windows-x64 artifact instead; building from source is not required.
"@
}

$BuildPython = ".\.venv-build\Scripts\python.exe"
if (Test-Path $BuildPython) {
    & $BuildPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Removing an invalid or outdated .venv-build environment."
        Remove-Item -Recurse -Force ".venv-build"
    }
}
if (-not (Test-Path $BuildPython)) {
    Invoke-NativeCommand $BootstrapPython @BootstrapArguments -m venv .venv-build
}

Invoke-NativeCommand $BuildPython -m pip install --upgrade pip
Invoke-NativeCommand $BuildPython -m pip install -r requirements.txt "pyinstaller>=6.10,<7" "pywin32>=308"
if ($RunTests) {
    Write-Host "Running the optional full test suite..."
    Invoke-NativeCommand $BuildPython -m pip install pytest pytest-qt
    Invoke-NativeCommand $BuildPython -m pytest -q
} else {
    Write-Host "Skipping the optional test suite. Use scripts\build_windows.ps1 -RunTests to include it."
}
Invoke-NativeCommand $BuildPython scripts\verify_release.py
Invoke-NativeCommand $BuildPython -m PyInstaller --noconfirm --clean ImageForge.spec

$Executable = Resolve-Path "dist\ImageOptimizer.exe"
$SmokeData = Join-Path $env:TEMP ("ImageForge-smoke-" + [guid]::NewGuid().ToString("N"))
try {
    Invoke-NativeCommand $Executable --check-startup --project-data $SmokeData
} finally {
    if (Test-Path $SmokeData) { Remove-Item -Recurse -Force $SmokeData }
}

Write-Host "Built dist\ImageOptimizer.exe"
Write-Host "The packaged executable passed its startup check and can be opened by double-clicking it."
Write-Host "ProjectData is intentionally not packaged; it will be created beside the executable."
