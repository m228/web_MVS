# Creates a desktop shortcut "web_MVS панель": the panel opens in its own small window (Edge or Chrome in app mode: no tabs, no address bar).
# Usage:  make_shortcut.bat                          -> panel on this PC (http://127.0.0.1:8769/)
#         make_shortcut.bat https://monitor.example.ru/   -> panel by its domain (from any PC)
# The icon is copied to %LOCALAPPDATA%\web_MVS_panel, so the shortcut keeps working if this folder is moved.
param(
    [string]$Url = "http://127.0.0.1:8769/",
    [string]$Name = "web_MVS панель",
    [string]$OutDir = [Environment]::GetFolderPath("Desktop"),
    [int]$Width = 560,
    [int]$Height = 820
)
$ErrorActionPreference = "Stop"

$pf86 = ${env:ProgramFiles(x86)}
$candidates = @(
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "$pf86\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "$pf86\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$browser = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $browser) {
    Write-Host "Edge or Chrome not found. Open $Url in any browser and use 'Install as app' in its menu."
    exit 1
}

$srcIcon = Join-Path $PSScriptRoot "static\panel.ico"
$iconDir = Join-Path $env:LOCALAPPDATA "web_MVS_panel"
New-Item -ItemType Directory -Force -Path $iconDir | Out-Null
$icon = Join-Path $iconDir "panel.ico"
if (Test-Path $srcIcon) { Copy-Item -Force $srcIcon $icon }

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$lnkPath = Join-Path $OutDir ($Name + ".lnk")
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = $browser
$lnk.Arguments = "--app=$Url --window-size=$Width,$Height"
$lnk.WorkingDirectory = Split-Path $browser
$lnk.Description = "web_MVS панель: мониторинг заводов"
if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
$lnk.Save()
Write-Host "Shortcut created: $lnkPath"
Write-Host "Opens: $Url  (via $browser)"
