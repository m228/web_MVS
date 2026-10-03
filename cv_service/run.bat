@echo off
REM ==== Запуск CV-сайдкара (распознавание кристаллов) ====
REM Порт 8765 (localhost). Модель: model\best.pt если есть, иначе classic (без GPU).
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo venv не найден. Сначала запусти setup.bat
  pause & exit /b 1
)

if exist "model\best.pt" (
  set "CV_MODEL=model\best.pt"
  echo Модель: model\best.pt
) else (
  set "CV_MODEL=classic"
  echo Модель не найдена -^> режим classic ^(без GPU^). Залей .pt через кнопку в UI или положи в model\best.pt
)

set "CV_PORT=8765"
set "CV_HOST=127.0.0.1"
.venv\Scripts\python.exe server.py
pause
