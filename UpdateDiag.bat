@echo off
rem Update diagnostic: why the app vanishes after an update. Read-only. Report is saved as update_diag_*.txt next to this file - send it.
cd /d "%~dp0"
if not exist "UpdateDiag.ps1" (
    echo [update-diag] UpdateDiag.ps1 not found next to this script.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0UpdateDiag.ps1" %*
pause
