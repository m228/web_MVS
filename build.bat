@echo off
rem Build the web_MVS bundle on the build machine (needs Python 3.11 + .venv with deps).
rem Output: dist\web_MVS\ and dist\web_MVS_v<version>.zip for GitHub Releases.
rem After the build release.bat checks the tree and, after a Y/N question, publishes the release. "build.bat nopush" skips that.
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

echo [build] Adding run.bat + update + UpdaterCV to the bundle ...
copy /Y run.bat "dist\web_MVS\" >nul
copy /Y UpdaterCV.bat "dist\web_MVS\" >nul
copy /Y UpdaterCV.ps1 "dist\web_MVS\" >nul
copy /Y SelfCheck_HW.bat "dist\web_MVS\" >nul
copy /Y update.bat "dist\web_MVS\" >nul
copy /Y update.ps1 "dist\web_MVS\" >nul

echo [build] Packing main archive ...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\web_MVS\*' -DestinationPath 'dist\web_MVS_v%VER%.zip' -Force" || exit /b 1

echo [build] Packing CV add-on (cv_service, code only) ...
copy /Y VERSION "cv_service\VERSION" >nul
powershell -NoProfile -Command "$f=Get-ChildItem 'cv_service' -Recurse -File | Where-Object { $_.FullName -notmatch '\\\.venv\\' -and $_.FullName -notmatch '\\model\\' -and $_.FullName -notmatch '__pycache__' }; Compress-Archive -Path $f.FullName -DestinationPath 'dist\cv_service_v%VER%.zip' -Force" || exit /b 1

echo.
echo [build] Done:
echo         dist\web_MVS_v%VER%.zip      (основное приложение, для всех)
echo         dist\cv_service_v%VER%.zip   (CV-модуль, докачивается UpdaterCV.bat на микроскопных машинах)
if /i "%~1"=="nopush" (
    echo [build] nopush: release not published. Publish later with release.bat
    endlocal
    exit /b 0
)
echo [build] Publishing release (checks + confirmation) ...
call release.bat
set RC=%errorlevel%
endlocal & exit /b %RC%
