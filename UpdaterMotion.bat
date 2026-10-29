@echo off
REM ==== Update Motion - download/update the camera motion-analysis service ====
REM Run manually. Downloads motion_service from the latest GitHub release; first run also
REM installs its venv (OpenCV, NumPy, pymodbus - setup.bat). No GPU needed.
REM GitHub file downloads blocked here? Get motion_service_v*.zip on another PC and run:
REM   UpdaterMotion.bat -ZipPath "D:\motion_service_v2.0.0.zip"
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0UpdaterMotion.ps1" %*
if errorlevel 1 (
  echo.
  echo ERROR: motion update failed. Check internet / GitHub access from this machine.
  pause & exit /b 1
)

if not exist "motion_service\.venv\Scripts\python.exe" (
  echo.
  echo First-time motion install ^(venv + OpenCV + NumPy + pymodbus^)...
  call "motion_service\setup.bat"
) else (
  echo.
  echo venv already present. If dependencies changed, run motion_service\setup.bat manually.
)

echo.
echo === Done. Start: motion_service\run.bat ===
echo If the motion service is already running, close its window and start run.bat again.
pause
