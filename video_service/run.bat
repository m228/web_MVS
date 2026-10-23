@echo off
REM ==== Start the video service (boil video) ====
REM Port 8767 (localhost). web_MVS sees it online and shows the "Video" tab on the microscope page.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo venv not found. Run setup.bat first.
  pause & exit /b 1
)

set "VIDEO_PORT=8767"
set "VIDEO_HOST=127.0.0.1"
.venv\Scripts\python.exe server.py
pause
