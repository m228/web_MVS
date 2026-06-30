@echo off
rem Launch web_MVS. Lives next to web_MVS.exe (in the unzipped release folder).
rem Elevates to admin for network features (jumbo frames, GigE filter driver).
cd /d "%~dp0"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [run] Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"
if not exist "web_MVS.exe" (
    echo [run] web_MVS.exe not found next to this script. Unzip the release here first.
    pause
    exit /b 1
)

"%~dp0web_MVS.exe"
pause
