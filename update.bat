@echo off
rem Update web_MVS from the latest GitHub release (also first-time install).
rem Shows installed and latest version, stops the app, replaces files, starts it again and waits until it answers.
rem Lives in the install root next to run.bat. User data (dataset\, Videos\, cv_history\, plate_config.json ...) is kept.
rem Options (pass after the name): -Force  -ZipPath "D:\web_MVS_v1.8.39.zip"  -NoRestart  -Port 8000
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" %*
pause
