@echo off
REM ==== Desktop shortcut "web_MVS panel": opens the monitoring panel in its own small window ====
REM   make_shortcut.bat                               - panel on this PC (http://127.0.0.1:8769/)
REM   make_shortcut.bat https://monitor.example.ru/   - panel by its domain
if "%~1"=="" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_shortcut.ps1"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_shortcut.ps1" -Url "%~1"
)
pause
