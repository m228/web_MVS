@echo off
REM ==== Install the level service (YOLO model -> contours for motion_service) ====
REM Creates a dedicated venv and installs ultralytics + torch. Default: CPU (no GPU needed).
REM   setup.bat        - CPU build (Windows PyPI torch is CPU-only)
REM   setup.bat gpu    - then replaces torch with the CUDA build (cu124; needs a recent NVIDIA driver)
REM Requires: Python 3.10+ in PATH. Picks up the system proxy for pip (same as cv_service\setup.bat).
setlocal
cd /d "%~dp0"

echo [0/4] Detecting system proxy ...
set "PX="
REM Find proxy: WinINET registry (browser) -> env vars; always normalize scheme to http://
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "$p=''; $s=Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue; if($s.ProxyEnable -eq 1 -and $s.ProxyServer){$p=$s.ProxyServer; if($p -match 'https?=([^;]+)'){$p=$matches[1]}}; if(-not $p){foreach($v in @($env:HTTPS_PROXY,$env:HTTP_PROXY,[Environment]::GetEnvironmentVariable('HTTPS_PROXY','Machine'),[Environment]::GetEnvironmentVariable('HTTP_PROXY','Machine'))){if($v){$p=$v;break}}}; if($p){$p=$p -replace '^https?://',''; 'http://'+$p}else{''}"`) do set "PX=%%p"
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

echo [3/4] ultralytics + torch ^(YOLO26 models need ultralytics 8.4+^) ...
pip install "ultralytics>=8.4"
if errorlevel 1 ( echo ERROR: ultralytics install failed ^(see above^). & pause & exit /b 1 )

if /i "%~1"=="gpu" (
  echo [4/4] torch + CUDA ^(cu124^) - replaces the CPU build ...
  pip install --force-reinstall torch torchvision --index-url https://download.pytorch.org/whl/cu124
  if errorlevel 1 ( echo ERROR: torch CUDA install failed ^(see above^). & pause & exit /b 1 )
) else (
  echo [4/4] CPU build of torch - nothing more to install.
)

echo.
python -c "import torch, ultralytics; print('torch', torch.__version__, '| ultralytics', ultralytics.__version__, '| CUDA:', torch.cuda.is_available())"
if errorlevel 1 (
  echo.
  echo NOTE: if torch failed with "c10.dll ... WinError 1114", install Microsoft
  echo       Visual C++ Redistributable x64: https://aka.ms/vs/17/release/vc_redist.x64.exe
)
echo.
echo Done. web_MVS starts the service itself; upload the model on the Level tab of the KZ panel.
endlocal
pause
