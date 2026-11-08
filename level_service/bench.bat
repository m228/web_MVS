@echo off
REM ==== Speed test of the level model: torch CPU vs OpenVINO CPU vs Intel iGPU ====
REM   bench.bat                                   - model from model\last_model.json, inputs 640 960 1280
REM   bench.bat model\26x_detection.pt 640 1280 C:\frame.jpg
REM Stop the level service first (so they do not compete for the CPU). Result: bench_result.txt
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo venv not found. Run setup.bat first.
  pause & exit /b 1
)

.venv\Scripts\python.exe bench.py %*
pause
