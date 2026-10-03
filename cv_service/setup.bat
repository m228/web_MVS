@echo off
REM ==== Install CV sidecar on the recognition machine (Bui / Kir) ====
REM Creates a dedicated venv and installs torch-CUDA + ultralytics + base deps.
REM Requires: Python 3.10+ in PATH and a recent NVIDIA driver (check: nvidia-smi).
REM CUDA Toolkit is NOT needed separately - the torch wheels bring their own cuda dlls.
cd /d "%~dp0"

echo [1/4] Creating venv .venv ...
python -m venv .venv
if errorlevel 1 ( echo ERROR: venv not created. Is python in PATH? & pause & exit /b 1 )

echo [2/4] Base dependencies ...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

echo [3/4] torch + CUDA (cu124) ...
pip install torch --index-url https://download.pytorch.org/whl/cu124

echo [4/4] ultralytics ...
pip install ultralytics

echo.
echo === GPU check ===
python -c "import torch; print('CUDA:', torch.cuda.is_available(), (torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NO GPU'))"
echo.
echo Done. Put model into model\best.pt (or upload via UI) and run run.bat
pause
