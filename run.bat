@echo off
rem Launch web_MVS. The exe carries a requireAdministrator manifest, so Windows
rem elevates it automatically (one UAC prompt). Double-clicking web_MVS.exe works too.
cd /d "%~dp0"
if not exist "web_MVS.exe" (
    echo [run] web_MVS.exe not found next to this script. Unzip the release here first.
    pause
    exit /b 1
)
start "" "%~dp0web_MVS.exe"
