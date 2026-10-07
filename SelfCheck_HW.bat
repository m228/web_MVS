@echo off
rem Selfcheck of ALL modules incl. live hardware polling (read-only): GigE cameras, plate, PLC, CV service, RTSP.
rem Without hardware those items show SKIP. Plain double-click on SelfCheck.exe = check without hardware.
cd /d "%~dp0"
if not exist "SelfCheck.exe" (
    echo [selfcheck] SelfCheck.exe not found next to this script.
    pause
    exit /b 1
)
SelfCheck.exe --hw
