@echo off
rem Update (and first-time install) of web_MVS from the latest GitHub release.
rem Lives in the install root next to run.bat. User data (dataset\, Videos\,
rem rtsp_cameras.json) is preserved - only the app\ folder is replaced.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1"
pause
