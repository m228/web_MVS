@echo off
rem Фейк-стенд web_MVS: приложение + эмулятор платы/ПЛК + сайдкары на синтетике, без железа. http://127.0.0.1:8011
rem Python: .venv рядом (или в корне основного репозитория), иначе системный. Параметры: fake.bat --cycle 30 --reset
cd /d "%~dp0"
set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if exist "..\..\..\.venv\Scripts\python.exe" set PY=..\..\..\.venv\Scripts\python.exe
%PY% scripts\fake_server.py --open %*
pause
