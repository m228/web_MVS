@echo off
rem Publish the built archives as a GitHub Release (called at the end of build.bat, or run by hand).
rem   release.bat        - checks, asks "Y/N", then gh release create
rem   release.bat dry    - only checks and prints what would be published
rem Factory machines update from the "Latest" release, so nothing is published unless:
rem   branch is main, tree is clean, main == origin/main, tag v<VERSION> is free, both zips exist.
setlocal
cd /d "%~dp0"

set /p VER=<VERSION
set "TAG=v%VER%"
set "ZIP1=dist\web_MVS_v%VER%.zip"
set "ZIP2=dist\cv_service_v%VER%.zip"

where gh >nul 2>&1 || (echo [release] gh CLI not found - install GitHub CLI and run "gh auth login" & exit /b 1)

set "BR="
for /f "delims=" %%b in ('git branch --show-current') do set "BR=%%b"
if /i not "%BR%"=="main" (echo [release] STOP: branch is "%BR%", not main. Merge PR into main first. & exit /b 1)

set "DIRTY="
for /f "delims=" %%x in ('git status --porcelain') do set "DIRTY=1"
if defined DIRTY (echo [release] STOP: uncommitted changes in the tree. Commit or stash them. & exit /b 1)

git fetch origin main -q || (echo [release] STOP: git fetch failed & exit /b 1)
set "LOC=" & set "RMT="
for /f %%h in ('git rev-parse HEAD') do set "LOC=%%h"
for /f %%h in ('git rev-parse origin/main') do set "RMT=%%h"
if not "%LOC%"=="%RMT%" (echo [release] STOP: local main differs from origin/main - git pull or git push first. & exit /b 1)

gh release view %TAG% >nul 2>&1
if not errorlevel 1 (echo [release] STOP: release %TAG% already exists - bump VERSION. & exit /b 1)

if not exist "%ZIP1%" (echo [release] STOP: %ZIP1% not found - run build.bat first. & exit /b 1)
if not exist "%ZIP2%" (echo [release] STOP: %ZIP2% not found - run build.bat first. & exit /b 1)

echo [release] Ready to publish %TAG% from main (%LOC:~0,7%):
echo           %ZIP1%
echo           %ZIP2%
echo [release] Factory machines (Bui, Kir) will pick it up as "Latest".
if /i "%~1"=="dry" (echo [release] dry run - nothing published. & exit /b 0)

choice /c YN /n /m "Publish release %TAG% to GitHub? [Y/N] "
if errorlevel 2 (echo [release] cancelled - nothing published. & exit /b 0)

gh release create %TAG% "%ZIP1%" "%ZIP2%" -t %TAG% --generate-notes || (echo [release] FAILED & exit /b 1)
echo [release] Published %TAG%.
endlocal
