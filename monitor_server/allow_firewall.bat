@echo off
REM ==== Windows Firewall: allow the monitoring receiver ONLY from the ZeroTier subnet (asks for admin rights) ====
REM   allow_firewall.bat                         - subnet is taken from data\server.json (host, last octet -> /24)
REM   allow_firewall.bat -Subnet 11.11.11.0/24
REM   allow_firewall.bat -Remove                 - delete the rule
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0allow_firewall.ps1" %*
pause
