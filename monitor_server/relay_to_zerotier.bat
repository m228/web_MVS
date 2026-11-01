@echo off
REM ==== Relay for plant PCs without direct internet: forwards a LAN port of THIS PC to the monitoring receiver over ZeroTier ====
REM   relay_to_zerotier.bat -Listen 192.168.1.5            (LAN address of this PC; run it on a PC that has ZeroTier)
REM   relay_to_zerotier.bat -Listen 192.168.1.5 -Remove
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0relay_to_zerotier.ps1" %*
pause
