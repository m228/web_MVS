@echo off
REM ==== Start the monitoring receiver (plant pulses -> panel) ====
REM Listens on 127.0.0.1:8769. Cloudflare Tunnel (or a reverse proxy with HTTPS) publishes it - see README.md.
REM Data: data\monitor.db. Panel password: data\server.json (created on first start) or env MONITOR_PASSWORD.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo venv not found. Run setup.bat first.
  pause & exit /b 1
)

set "MONITOR_PORT=8769"
set "MONITOR_HOST=127.0.0.1"
.venv\Scripts\python.exe server.py
pause
