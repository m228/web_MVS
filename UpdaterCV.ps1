# Качает последнюю версию CV-модуля (cv_service) из релиза GitHub и распаковывает рядом.
# Вызывается из UpdaterCV.bat. Не трогает cv_service\.venv и cv_service\model (их нет в zip).
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
$h = @{ 'User-Agent' = 'web_MVS-updatecv' }
$repo = 'm228/web_MVS'

Write-Host '=== Update CV: беру последнюю версию CV-модуля с GitHub ===' -ForegroundColor Cyan
$rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers $h
$asset = $rel.assets | Where-Object { $_.name -like 'cv_service*.zip' } | Select-Object -First 1
if (-not $asset) { throw 'В последнем релизе нет файла cv_service_*.zip' }

Write-Host ("Релиз: {0}  |  файл: {1}  ({2:N1} КБ)" -f $rel.tag_name, $asset.name, ($asset.size / 1KB))
$tmp = Join-Path $env:TEMP 'cv_service_dl.zip'
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $tmp -Headers $h

$dest = Join-Path $dir 'cv_service'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Expand-Archive -Path $tmp -DestinationPath $dest -Force
Remove-Item $tmp -Force
Write-Host "CV-модуль распакован в $dest" -ForegroundColor Green
