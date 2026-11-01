@echo off
REM ==== Start the receiver with a tray icon (hidden window, no console) - manual start; autostart does the same via install_autostart.bat ====
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo venv not found. Run setup.bat first.
  pause & exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" tray.py
