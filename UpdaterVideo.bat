@echo off
REM ==== Update Video - download/update the boil video module (microscope) ====
REM Run manually. Downloads video_service from the latest GitHub release; first run also
REM installs its venv (OpenCV, Pillow, ffmpeg - setup.bat). No GPU needed.
REM GitHub file downloads blocked here? Get video_service_v*.zip on another PC and run:
REM   UpdaterVideo.bat -ZipPath "D:\video_service_v1.9.85.zip"
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0UpdaterVideo.ps1" %*
if errorlevel 1 (
  echo.
  echo ERROR: video update failed. Check internet / GitHub access from this machine.
  pause & exit /b 1
)

if not exist "video_service\.venv\Scripts\python.exe" (
  echo.
  echo First-time video install ^(venv + OpenCV + Pillow + ffmpeg^)...
  call "video_service\setup.bat"
) else (
  echo.
  echo venv already present. If dependencies changed, run video_service\setup.bat manually.
)

echo.
echo === Done. Start: video_service\run.bat ===
echo If the video service is already running, close its window and start run.bat again.
pause
