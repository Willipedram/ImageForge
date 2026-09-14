@echo off
setlocal
cd /d "%~dp0"

echo Building the click-to-run ImageOptimizer.exe...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_windows.ps1"
if errorlevel 1 (
    echo.
    echo Build failed. Review the messages above.
    pause
    exit /b 1
)

echo.
echo Build complete: %~dp0dist\ImageOptimizer.exe
explorer.exe /select,"%~dp0dist\ImageOptimizer.exe"
pause

