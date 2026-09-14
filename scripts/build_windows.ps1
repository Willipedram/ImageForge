[CmdletBinding()]
param(
    [switch]$RunTests,
    [switch]$Clean,
    [switch]$RefreshDependencies
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

$DependencyStamp = ".venv-build\.imageforge-packaging-dependencies"
$DependencyKey = "v2|$((Get-FileHash requirements.txt -Algorithm SHA256).Hash)|pyinstaller>=6.10,<7|pywin32>=308"
$DependenciesReady = (-not $RefreshDependencies) -and (Test-Path $DependencyStamp) -and `
    ((Get-Content $DependencyStamp -Raw).Trim() -eq $DependencyKey)
if ($DependenciesReady) {
    & $BuildPython -c "import PIL, PyInstaller, PySide6, paramiko, psutil, pymysql, win32cred" *> $null
    $DependenciesReady = $LASTEXITCODE -eq 0
}
if ($DependenciesReady) {
    Write-Host "Dependencies unchanged; using the cached build environment."
} else {
    Write-Host "Installing packaging dependencies (first build or requirements changed)..."
    Invoke-NativeCommand $BuildPython -m pip install --disable-pip-version-check --prefer-binary `
        -r requirements.txt "pyinstaller>=6.10,<7" "pywin32>=308"
    Set-Content -Path $DependencyStamp -Value $DependencyKey -Encoding ascii
}
if ($RunTests) {
    Write-Host "Running the optional full test suite..."
    $DevStamp = ".venv-build\.imageforge-test-dependencies"
    $DevKey = "v1|$((Get-FileHash requirements-dev.txt -Algorithm SHA256).Hash)"
    if (-not (Test-Path $DevStamp) -or ((Get-Content $DevStamp -Raw).Trim() -ne $DevKey)) {
        Invoke-NativeCommand $BuildPython -m pip install --disable-pip-version-check --prefer-binary `
            -r requirements-dev.txt
        Set-Content -Path $DevStamp -Value $DevKey -Encoding ascii
    }
    Invoke-NativeCommand $BuildPython -m pytest -q
} else {
    Write-Host "Skipping the optional test suite. Use scripts\build_windows.ps1 -RunTests to include it."
}
if (-not (Test-Path ".git")) {
    Write-Host "Source ZIP detected (no .git directory); Git is not required."
}
Invoke-NativeCommand $BuildPython scripts\verify_release.py
$PyInstallerArguments = @("-m", "PyInstaller", "--noconfirm")
if ($Clean) {
    Write-Host "Clean build requested; PyInstaller caches will be rebuilt."
    $PyInstallerArguments += "--clean"
} else {
    Write-Host "Incremental build enabled; reusing unchanged PyInstaller analysis caches."
}
$PyInstallerArguments += "ImageForge.spec"
Invoke-NativeCommand $BuildPython @PyInstallerArguments

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
