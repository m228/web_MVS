@echo off
REM ==== Update CV - download/update the computer-vision module (microscope) ====
REM Run manually. Downloads cv_service from the latest GitHub release; first run also
REM installs torch + ultralytics (setup.bat). Not needed for camera/RTSP-only machines.
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0UpdaterCV.ps1"
if errorlevel 1 (
  echo.
  echo ERROR: CV update failed. Check internet / GitHub access from this machine.
  pause & exit /b 1
)

if not exist "cv_service\.venv\Scripts\python.exe" (
  echo.
  echo First-time CV install ^(venv + torch-CUDA + ultralytics^)...
  call "cv_service\setup.bat"
) else (
  echo.
  echo venv already present. If dependencies changed, run cv_service\setup.bat manually.
)

echo.
echo === Done. Start CV: cv_service\run.bat ===
echo Put model into cv_service\model\best.pt or upload it via the "Load model" button in UI.
pause
