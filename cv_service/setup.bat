@echo off
REM ==== Install CV sidecar on the recognition machine (Bui / Kir) ====
REM Creates a dedicated venv and installs torch-CUDA + ultralytics + base deps.
REM Requires: Python 3.10+ in PATH and a recent NVIDIA driver (check: nvidia-smi).
REM CUDA Toolkit is NOT needed separately - the torch wheels bring their own cuda dlls.
REM Auto-detects the system proxy and feeds it to pip as http:// (avoids the Python 3.10
REM "check_hostname requires server_hostname" bug that happens with an https:// proxy).
setlocal
cd /d "%~dp0"

echo [0/4] Detecting system proxy ...
set "PX="
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "$s=Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue; if($s.ProxyEnable -eq 1 -and $s.ProxyServer){ $ps=$s.ProxyServer; if($ps -match 'https?=([^;]+)'){$ps=$matches[1]}; $ps=$ps -replace '^https?://',''; 'http://'+$ps } else {''}"`) do set "PX=%%p"
if defined PX (
  echo Using system proxy: %PX%
  set "HTTP_PROXY=%PX%"
  set "HTTPS_PROXY=%PX%"
  set "PIP_PROXY=%PX%"
) else (
  echo No system proxy configured - direct connection.
)

echo [1/4] Creating venv .venv ...
python -m venv .venv
if errorlevel 1 ( echo ERROR: venv not created. Is python in PATH? & pause & exit /b 1 )

echo [2/4] Base dependencies ...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 ( echo ERROR: base deps failed ^(see above^). If it is a proxy/SSL error, check the proxy. & pause & exit /b 1 )

echo [3/4] torch + CUDA (cu124) ...
pip install torch --index-url https://download.pytorch.org/whl/cu124
if errorlevel 1 ( echo ERROR: torch install failed ^(see above^). & pause & exit /b 1 )

echo [4/4] ultralytics ...
pip install ultralytics
if errorlevel 1 ( echo ERROR: ultralytics install failed ^(see above^). & pause & exit /b 1 )

echo.
echo === GPU check ===
python -c "import torch; print('CUDA:', torch.cuda.is_available(), (torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NO GPU'))"
echo.
echo Done. Put model into model\best.pt (or upload via UI) and run run.bat
endlocal
pause
