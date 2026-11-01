@echo off
REM ==== Connect Telegram: bot token + chat are entered here (hidden), saved to data\server.json (not in git) ====
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo venv not found. Run setup.bat first.
  pause & exit /b 1
)
.venv\Scripts\python.exe telegram_setup.py
pause
