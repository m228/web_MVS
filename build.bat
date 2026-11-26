@echo off
rem Build the web_MVS bundle on the build machine (needs Python 3.11 + .venv with deps).
rem Partial release: only components whose version is higher than in the previous release manifest are published
rem (VERSION = main app, component_versions.json = cv/video/motion/level). The main app (PyInstaller, ~90 MB) is built only if its VERSION was raised.
rem Output: dist\<component>_v<its version>.zip + dist\components.json + dist\release_files.txt (what release.bat publishes).
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

if not exist "dist" mkdir "dist"
rem leftovers of an earlier build must not reach the release
del /q "dist\*.zip" "dist\components.json" "dist\release_files.txt" "dist\release_notes.txt" "dist\plan.cmd" "dist\prev_components.json" >nul 2>&1

rem what changed: compare component versions with the manifest of the latest release (fetched via gh; no manifest = build everything)
gh release download --pattern components.json --output "dist\prev_components.json" --clobber >nul 2>&1
if not exist "dist\prev_components.json" echo [build] WARNING: previous release manifest NOT fetched (gh missing, not logged in, no network?) - building EVERYTHING, change checks skipped.
python scripts\make_manifest.py --plan "dist\plan.cmd" --prev "dist\prev_components.json" || (echo [build] PLAN FAILED & exit /b 1)
call "dist\plan.cmd"
set /p REL=<RELEASE

if "%BUILD_main%"=="0" (
    echo [build] Main app %VER_main% is unchanged - PyInstaller build and main archive skipped.
    goto services
)

python -m pip install --upgrade pyinstaller || exit /b 1

rem enrollment key for monitoring auto-registration: same key as the receiver (monitor_server\data\server.json); kept out of git
if exist "monitor_server\data\server.json" powershell -NoProfile -Command "try { $k = (Get-Content 'monitor_server\data\server.json' -Raw -Encoding UTF8 | ConvertFrom-Json).enroll_key; if ($k) { [IO.File]::WriteAllText('enroll_key.txt', $k.Trim()) } } catch { }"
if exist "enroll_key.txt" (echo [build] enroll_key.txt found: auto-registration will work in this build) else (echo [build] WARNING: no enroll_key.txt - this build will NOT auto-register on the monitor. Start the receiver once, then rebuild.)

echo [build] Building web_MVS %VER_main% ...
pyinstaller --noconfirm web_MVS.spec || (echo [build] BUILD FAILED & exit /b 1)

echo [build] Adding run.bat + update + UpdaterCV + UpdaterVideo + UpdaterMotion + UpdaterLevel to the bundle ...
copy /Y run.bat "dist\web_MVS\" >nul
copy /Y UpdaterCV.bat "dist\web_MVS\" >nul
copy /Y UpdaterCV.ps1 "dist\web_MVS\" >nul
copy /Y UpdaterVideo.bat "dist\web_MVS\" >nul
copy /Y UpdaterVideo.ps1 "dist\web_MVS\" >nul
copy /Y UpdaterMotion.bat "dist\web_MVS\" >nul
copy /Y UpdaterMotion.ps1 "dist\web_MVS\" >nul
copy /Y UpdaterLevel.bat "dist\web_MVS\" >nul
copy /Y UpdaterLevel.ps1 "dist\web_MVS\" >nul
copy /Y SelfCheck_HW.bat "dist\web_MVS\" >nul
copy /Y NetDiag.bat "dist\web_MVS\" >nul
copy /Y UpdateDiag.bat "dist\web_MVS\" >nul
copy /Y UpdateDiag.ps1 "dist\web_MVS\" >nul
copy /Y update.bat "dist\web_MVS\" >nul
copy /Y update.ps1 "dist\web_MVS\" >nul
copy /Y Uninstall.bat "dist\web_MVS\" >nul
copy /Y Uninstall.ps1 "dist\web_MVS\" >nul

echo [build] Packing main archive ...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\web_MVS\*' -DestinationPath 'dist\web_MVS_v%VER_main%.zip' -Force" || exit /b 1

:services
rem Services are packed every time (cheap): the manifest step compares their content with the previous release and stops if the code
rem changed but the version was not raised. Archives of unchanged services are then dropped from dist\ and are not published.
rem service versions come from component_versions.json (not from VERSION): write them into the service folders before packing
python scripts\make_manifest.py --write-versions || (echo [build] write-versions FAILED & exit /b 1)
echo [build] Packing CV add-on (cv_service, code only) ...
powershell -NoProfile -Command "$f=Get-ChildItem 'cv_service' -Recurse -File | Where-Object { $_.FullName -notmatch '\\\.venv\\' -and $_.FullName -notmatch '\\model\\' -and $_.FullName -notmatch '__pycache__' -and $_.Name -notlike 'service.log*' }; Compress-Archive -Path $f.FullName -DestinationPath 'dist\cv_service_v%VER_cv%.zip' -Force" || exit /b 1

rem top-level items, not a flat file list: assets\ must keep its folder in the zip
echo [build] Packing video add-on (video_service, code only) ...
powershell -NoProfile -Command "$f=Get-ChildItem 'video_service' | Where-Object { $_.Name -notin @('.venv','venv','video','__pycache__') -and $_.Name -notlike 'service.log*' }; Compress-Archive -Path $f.FullName -DestinationPath 'dist\video_service_v%VER_video%.zip' -Force" || exit /b 1

rem same: top-level items; data\ (camera settings) and .venv must never go into the zip
echo [build] Packing motion add-on (motion_service, code only) ...
powershell -NoProfile -Command "$f=Get-ChildItem 'motion_service' | Where-Object { $_.Name -notin @('.venv','venv','data','__pycache__','tests') -and $_.Name -notlike 'service.log*' }; Compress-Archive -Path $f.FullName -DestinationPath 'dist\motion_service_v%VER_motion%.zip' -Force" || exit /b 1

rem same: top-level items; .venv, model\ (uploaded YOLO models) and logs must never go into the zip
echo [build] Packing level add-on (level_service, code only) ...
powershell -NoProfile -Command "$f=Get-ChildItem 'level_service' | Where-Object { $_.Name -notin @('.venv','venv','model','__pycache__','bench_ov','bench_result.txt') -and $_.Name -notlike 'service.log*' }; Compress-Archive -Path $f.FullName -DestinationPath 'dist\level_service_v%VER_level%.zip' -Force" || exit /b 1

rem manifest: per-component versions (+ tag/asset: which release holds each archive), check that a changed service got a new version, list of files to publish
python scripts\make_manifest.py --dist dist --prev "dist\prev_components.json" || (echo [build] MANIFEST CHECK FAILED & exit /b 1)
rem unchanged components point to archives in older releases: they must still exist there (never delete releases that components.json refers to)
python scripts\make_manifest.py --verify --dist dist || (echo [build] MANIFEST VERIFY FAILED & exit /b 1)
echo.
echo [build] Done. Release v%REL% will publish only the changed components:
type "dist\release_files.txt"
echo [build] (components.json lists the version and the release of every component: clients update only what is behind)
if /i "%~1"=="nopush" (
    echo [build] nopush: release not published. Publish later with release.bat
    endlocal
    exit /b 0
)
echo [build] Publishing release (checks + confirmation) ...
call release.bat
set RC=%errorlevel%
endlocal & exit /b %RC%
