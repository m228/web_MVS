@echo off
rem Build the web_MVS bundle on the build machine (needs Python 3.11 + .venv with deps).
rem Output: dist\web_MVS\ and dist\web_MVS_v<version>.zip for GitHub Releases.
rem run.bat is bundled INTO the archive so the zip is self-sufficient to run.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo [build] No .venv found. Create it and install deps:
    echo         py -3.11 -m venv .venv
    echo         .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)
call ".venv\Scripts\activate.bat"

python -m pip install --upgrade pyinstaller || exit /b 1

set /p VER=<VERSION
echo [build] Building web_MVS %VER% ...
pyinstaller --noconfirm web_MVS.spec || (echo [build] BUILD FAILED & exit /b 1)

echo [build] Adding run.bat to the bundle ...
copy /Y run.bat "dist\web_MVS\" >nul

echo [build] Packing archive ...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\web_MVS\*' -DestinationPath 'dist\web_MVS_v%VER%.zip' -Force" || exit /b 1

echo.
echo [build] Done: dist\web_MVS_v%VER%.zip
echo [build] Publish release:
echo         gh release create v%VER% dist\web_MVS_v%VER%.zip -t v%VER% --generate-notes
endlocal
