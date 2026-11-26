@echo off
REM ==== Start the level service (YOLO model -> contours for motion_service) ====
REM Port 8770 (localhost). Model: the last uploaded one (model\last_model.json), else model\best.pt if present.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo venv not found. Run setup.bat first.
  pause & exit /b 1
)

if exist "model\best.pt" (
  set "LEVEL_MODEL=model\best.pt"
  echo Fallback model: model\best.pt
) else (
  echo No fallback model. Upload a .pt in web_MVS ^(Level tab^) or put it in model\best.pt
)

set "LEVEL_PORT=8770"
set "LEVEL_HOST=127.0.0.1"
.venv\Scripts\python.exe server.py
pause
