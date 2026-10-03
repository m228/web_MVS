# Downloads the latest CV module (cv_service) from the GitHub release and unpacks it next to this script.
# Called by UpdaterCV.bat. Does NOT touch cv_service\.venv or cv_service\model (not in the zip).
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
$h = @{ 'User-Agent' = 'web_MVS-updatecv' }
$repo = 'm228/web_MVS'

Write-Host '=== Update CV: fetching latest CV module from GitHub ===' -ForegroundColor Cyan
$rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers $h
$asset = $rel.assets | Where-Object { $_.name -like 'cv_service*.zip' } | Select-Object -First 1
if (-not $asset) { throw 'No cv_service_*.zip asset in the latest release' }

Write-Host ("Release: {0}  |  file: {1}  ({2:N1} KB)" -f $rel.tag_name, $asset.name, ($asset.size / 1KB))
$tmp = Join-Path $env:TEMP 'cv_service_dl.zip'
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $tmp -Headers $h

$dest = Join-Path $dir 'cv_service'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Expand-Archive -Path $tmp -DestinationPath $dest -Force
Remove-Item $tmp -Force
Write-Host "CV module unpacked to $dest" -ForegroundColor Green
