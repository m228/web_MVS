@echo off
REM ==== Update Level - download/update the liquid-level service (YOLO model -> contours) ====
REM Run manually. Downloads level_service from the latest GitHub release; first run also
REM installs its venv (OpenCV, FastAPI, ultralytics + torch CPU - setup.bat; about 1-2 GB, takes a while).
REM Needs Python 3.10+ in PATH. For a PC with an NVIDIA GPU run level_service\setup.bat gpu afterwards.
REM GitHub file downloads blocked here? Get level_service_v*.zip on another PC and run:
REM   UpdaterLevel.bat -ZipPath "D:\level_service_v2.1.36.zip"
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0UpdaterLevel.ps1" %*
if errorlevel 1 (
  echo.
  echo ERROR: level update failed. Check internet / GitHub access from this machine.
  pause & exit /b 1
)

if not exist "level_service\.venv\Scripts\python.exe" (
  echo.
  echo First-time level install ^(venv + OpenCV + FastAPI + ultralytics + torch^)...
  call "level_service\setup.bat"
) else (
  echo.
  echo venv already present. If dependencies changed, run level_service\setup.bat manually.
)

echo.
echo === Done. web_MVS starts the service itself; upload the YOLO model on the Level tab of the KZ panel. ===
echo Manual start: level_service\run.bat. If the service is already running, close its window and start run.bat again.
pause
