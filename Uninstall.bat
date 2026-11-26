@echo off
rem Remove add-on environments and/or Python (to re-test a clean install via update.bat). The main app (web_MVS.exe) is never touched.
rem Without options it shows a menu. Options: -Venvs  -Python  -Addons  -All  -Yes  (see Uninstall.ps1)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Uninstall.ps1" %*
pause
