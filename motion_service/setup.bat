@echo off
REM ==== Install the motion service (zone motion detection from IP cameras -> Modbus) ====
REM Creates a dedicated venv and installs deps (OpenCV, NumPy, FastAPI, pymodbus). No GPU needed.
REM Requires: Python 3.10+ in PATH. Picks up the system proxy for pip (same as video_service\setup.bat).
setlocal
cd /d "%~dp0"

echo [0/2] Detecting system proxy ...
set "PX="
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "$p=''; $s=Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue; if($s.ProxyEnable -eq 1 -and $s.ProxyServer){$p=$s.ProxyServer; if($p -match 'https?=([^;]+)'){$p=$matches[1]}}; if(-not $p){foreach($v in @($env:HTTPS_PROXY,$env:HTTP_PROXY,[Environment]::GetEnvironmentVariable('HTTPS_PROXY','Machine'),[Environment]::GetEnvironmentVariable('HTTP_PROXY','Machine'))){if($v){$p=$v;break}}}; if($p){$p=$p -replace '^https?://',''; 'http://'+$p}else{''}"`) do set "PX=%%p"
if defined PX (
  echo Using system proxy: %PX%
  set "HTTP_PROXY=%PX%"
  set "HTTPS_PROXY=%PX%"
  set "PIP_PROXY=%PX%"
) else (
  echo No system proxy configured - direct connection.
)

echo [1/2] Creating venv .venv ...
python -m venv .venv
if errorlevel 1 ( echo ERROR: venv not created. Is python in PATH? & pause & exit /b 1 )

echo [2/2] Dependencies ...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 ( echo ERROR: deps failed ^(see above^). If it is a proxy/SSL error, check the proxy. & pause & exit /b 1 )

echo.
python -c "import cv2, numpy, pymodbus; print('opencv', cv2.__version__, '| numpy', numpy.__version__, '| pymodbus', pymodbus.__version__)"
echo.
echo Done. Run run.bat - web_MVS starts the service itself and shows the Motion page.
endlocal
pause
