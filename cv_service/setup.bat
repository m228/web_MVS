@echo off
REM ==== Установка CV-сайдкара на машине распознавания (Буи / Кир) ====
REM Делает отдельный venv и ставит torch-CUDA + ultralytics + базу.
REM Требуется: Python 3.10+ в PATH и свежий драйвер NVIDIA (проверь: nvidia-smi).
REM CUDA Toolkit отдельно НЕ нужен — колёса torch тащат свои cuda-длл.
chcp 65001 >nul
cd /d "%~dp0"

echo [1/4] Создаю venv .venv ...
python -m venv .venv
if errorlevel 1 ( echo ОШИБКА: не создался venv. Проверь, что python в PATH. & pause & exit /b 1 )

echo [2/4] Базовые зависимости ...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

echo [3/4] torch + CUDA (cu124) ...
pip install torch --index-url https://download.pytorch.org/whl/cu124

echo [4/4] ultralytics ...
pip install ultralytics

echo.
echo === Проверка GPU ===
python -c "import torch; print('CUDA:', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'НЕТ GPU')"
echo.
echo Готово. Положи модель в model\best.pt (или залей через кнопку в UI) и запусти run.bat
pause
