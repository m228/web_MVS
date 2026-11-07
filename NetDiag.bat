@echo off
rem Network diagnostic: why the monitor pulse does not reach the receiver. Report is saved as net_diag_*.txt next to this file.
cd /d "%~dp0"
if not exist "web_MVS.exe" (
    echo [net-diag] web_MVS.exe not found next to this script.
    pause
    exit /b 1
)
web_MVS.exe --net-diag %*
