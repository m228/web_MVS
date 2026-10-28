@echo off
REM ==== Start the motion service (zone motion detection -> Modbus) ====
REM Port 8768 (localhost). web_MVS sees it online and shows the "Motion" page. Settings: data\motion_config.json
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo venv not found. Run setup.bat first.
  pause & exit /b 1
)

set "MOTION_PORT=8768"
set "MOTION_HOST=127.0.0.1"
.venv\Scripts\python.exe server.py
pause
