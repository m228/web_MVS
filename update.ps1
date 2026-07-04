# Обновление web_MVS: качает последний релиз с GitHub и распаковывает его ПОВЕРХ
# этой папки, НЕ трогая пользовательские данные (dataset\, Videos\, rtsp_cameras.json).
# Работает и как первичная установка. Кладётся рядом с web_MVS.exe.
# Использует GitHub CLI (gh), если он есть; иначе — публичный GitHub API.
$ErrorActionPreference = 'Stop'
# на чистой Windows PowerShell 5.1 по умолчанию TLS 1.0/1.1 — GitHub требует 1.2
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$repo = 'm228/web_MVS'
$tmp = Join-Path $env:TEMP ('web_mvs_upd_' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

try {
    Write-Host '[update] Определяю последний релиз...'
    $zip = $null
    if (Get-Command gh -ErrorAction SilentlyContinue) {
        gh release download --repo $repo --pattern 'web_MVS_*.zip' --dir $tmp --clobber
        $zip = Get-ChildItem $tmp -Filter 'web_MVS_*.zip' | Select-Object -First 1
    } else {
        $rel = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -Headers @{ 'User-Agent' = 'web_MVS-update' }
        $asset = $rel.assets | Where-Object { $_.name -like 'web_MVS_*.zip' } | Select-Object -First 1
        if (-not $asset) { throw 'В последнем релизе нет файла web_MVS_*.zip' }
        $dest = Join-Path $tmp $asset.name
        Invoke-WebRequest $asset.browser_download_url -OutFile $dest
        $zip = Get-Item $dest
    }
    if (-not $zip) { throw 'Не удалось скачать архив релиза' }

    Write-Host '[update] Останавливаю запущенный web_MVS...'
    Get-Process web_MVS -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Milliseconds 500

    Write-Host '[update] Обновляю файлы приложения...'
    # чистим только файлы приложения; данные (dataset, Videos, rtsp_cameras.json) не трогаем
    Remove-Item (Join-Path $root '_internal') -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $root 'web_MVS.exe') -Force -ErrorAction SilentlyContinue
    Expand-Archive -Path $zip.FullName -DestinationPath $root -Force

    $ver = (Get-Content (Join-Path $root '_internal\VERSION') -ErrorAction SilentlyContinue) -join ''
    Write-Host "[update] Готово. Версия: $ver"
    Write-Host '[update] Запустите run.bat (от администратора).'
}
catch {
    Write-Host "[update] ОШИБКА: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host '[update] Проверьте, что релиз опубликован на GitHub и доступен с этой машины.'
    exit 1
}
finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
