@echo off
REM ==== Start CV sidecar (crystal recognition) ====
REM Port 8765 (localhost). Model: model\best.pt if present, else classic (no GPU).
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo venv not found. Run setup.bat first.
  pause & exit /b 1
)

if exist "model\best.pt" (
  set "CV_MODEL=model\best.pt"
  echo Model: model\best.pt
) else (
  set "CV_MODEL=classic"
  echo No model found -^> classic mode ^(no GPU^). Upload a .pt via UI or put it in model\best.pt
)

set "CV_PORT=8765"
set "CV_HOST=127.0.0.1"
.venv\Scripts\python.exe server.py
pause
