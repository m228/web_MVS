@echo off
REM ==== Update CV — докачать/обновить модуль компьютерного зрения (микроскоп) ====
REM Запускаешь руками. Качает cv_service из последнего релиза GitHub, при первом разе
REM ставит torch+ultralytics (setup.bat). Для камер/RTSP без микроскопа — НЕ нужен.
chcp 65001 >nul
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0UpdaterCV.ps1"
if errorlevel 1 (
  echo.
  echo ОШИБКА: не удалось обновить CV-модуль. Проверь интернет/доступ к GitHub.
  pause & exit /b 1
)

if not exist "cv_service\.venv\Scripts\python.exe" (
  echo.
  echo Первичная установка CV ^(venv + torch-CUDA + ultralytics^)...
  call "cv_service\setup.bat"
) else (
  echo.
  echo venv уже есть. Если менялись зависимости — запусти cv_service\setup.bat вручную.
)

echo.
echo === Готово. Запуск CV: cv_service\run.bat ===
echo Модель положи в cv_service\model\best.pt или залей кнопкой «Загрузить модель» в UI.
pause
