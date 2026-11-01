@echo off
REM ==== Autostart of the monitoring receiver (Task Scheduler, at logon, hidden window) ====
REM   install_autostart.bat            - create the task and start it now
REM   install_autostart.bat -Remove    - delete the task
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" %*
pause
