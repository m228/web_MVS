@echo off
rem Publish the built archives as a GitHub Release (called at the end of build.bat, or run by hand).
rem   release.bat        - checks, asks "Y/N", then gh release create
rem   release.bat dry    - only checks and prints what would be published
rem Partial release: it publishes only what build.bat listed in dist\release_files.txt (changed components + components.json).
rem Factory machines update from the "Latest" release, so nothing is published unless:
rem   branch is main, tree is clean, main == origin/main, tag v<RELEASE> is free, every listed file exists.
setlocal enabledelayedexpansion
cd /d "%~dp0"

set /p REL=<RELEASE
set "TAG=v%REL%"
set "LIST=dist\release_files.txt"
set "NOTES=dist\release_notes.txt"

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
if not errorlevel 1 (echo [release] STOP: release %TAG% already exists - bump RELEASE. & exit /b 1)

if not exist "%LIST%" (echo [release] STOP: %LIST% not found - run build.bat first. & exit /b 1)
if not exist "%NOTES%" (echo [release] STOP: %NOTES% not found - run build.bat first. & exit /b 1)
findstr /c:"\"release\": \"%REL%\"" "dist\components.json" >nul || (echo [release] STOP: dist\components.json was built for another RELEASE than %REL% - run build.bat again. & exit /b 1)

set "FILES="
echo [release] Ready to publish %TAG% from main (%LOC:~0,7%):
for /f "usebackq delims=" %%f in ("%LIST%") do (
    if not exist "%%f" (echo [release] STOP: %%f not found - run build.bat first. & exit /b 1)
    echo           %%f
    set FILES=!FILES! "%%f"
)
if not defined FILES (echo [release] STOP: %LIST% is empty. & exit /b 1)
echo [release] Factory machines (Bui, Kir) will pick it up as "Latest"; they download only the components that are behind.
if /i "%~1"=="dry" (echo [release] dry run - nothing published. & exit /b 0)

choice /c YN /n /m "Publish release %TAG% to GitHub? [Y/N] "
if errorlevel 2 (echo [release] cancelled - nothing published. & exit /b 0)

gh release create %TAG% !FILES! -t %TAG% --notes-file "%NOTES%" --generate-notes || (echo [release] FAILED & exit /b 1)
echo [release] Published %TAG%.
endlocal
