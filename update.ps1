# Обновление web_MVS одним запуском (update.bat): показывает установленную и последнюю версию, останавливает приложение,
# заменяет файлы, запускает его снова и ждёт, пока оно поднимется. Если новая версия не поднялась — откатывает старую.
# Данные (dataset\, Videos\, rtsp_cameras.json, cv_history\, cv_results\, plate_config.json) не трогает: заменяются только
# web_MVS.exe, _internal\ и файлы из архива (run.bat, UpdaterCV...). Работает и как первичная установка.
#
# Параметры (обычно не нужны):
#   -ZipPath <файл>   готовый архив web_MVS_v*.zip вместо скачивания (когда GitHub недоступен: скачали на другой машине)
#   -Force            переустановить, даже если версия уже последняя
#   -NoRestart        не запускать приложение после обновления
#   -Repo <владелец/репозиторий>  откуда брать релизы (по умолчанию m228/web_MVS)
#   -Port <порт>      порт приложения для проверки запуска (по умолчанию 8000)
#   -Root <папка>     папка установки (по умолчанию — рядом со скриптом)
#   -NoElevate        не запрашивать права администратора (для проверок)
[CmdletBinding()]
param(
    [string]$Root = '',
    [string]$ZipPath = '',
    [string]$Repo = 'm228/web_MVS',
    [int]$Port = 8000,
    [switch]$Force,
    [switch]$NoRestart,
    [switch]$NoElevate,
    [switch]$Elevated
)
$ErrorActionPreference = 'Stop'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}   # GitHub требует TLS 1.2
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$repo = $Repo
# папка установки: $PSScriptRoot в значении параметра при запуске через -File пуст (PowerShell 5.1), поэтому берём здесь
if ([string]::IsNullOrWhiteSpace($Root)) {
    if ($PSScriptRoot) { $Root = $PSScriptRoot }
    elseif ($PSCommandPath) { $Root = Split-Path -Parent $PSCommandPath }
    elseif ($MyInvocation.MyCommand.Path) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
    else { $Root = (Get-Location).Path }
}
$Root = (Resolve-Path -LiteralPath $Root).Path
$logFile = Join-Path $Root 'update.log'
$script:exitCode = 0

function Log([string]$msg, [string]$color = '') {
    $line = "[update] $msg"
    if ($color) { Write-Host $line -ForegroundColor $color } else { Write-Host $line }
    try { ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg) | Out-File -FilePath $logFile -Append -Encoding utf8 } catch {}
}

# ---------- права администратора: exe требует их, а остановить запущенный exe можно только от админа ----------
function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
if (-not $NoElevate -and -not (Test-Admin)) {
    Write-Host '[update] Нужны права администратора — запрашиваю (подтвердите окно Windows)...'
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Elevated', '-Root', "`"$Root`"", '-Port', $Port)
    if ($ZipPath) { $argList += @('-ZipPath', "`"$ZipPath`"") }
    if ($Force) { $argList += '-Force' }
    if ($NoRestart) { $argList += '-NoRestart' }
    try {
        $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs -Wait -PassThru
        exit $p.ExitCode
    } catch {
        Write-Host '[update] ОШИБКА: права администратора не получены. Запустите update.bat «от имени администратора».' -ForegroundColor Red
        exit 1
    }
}

# ---------- версии ----------
function Test-VersionString([string]$v) { return ($v -match '^\d+(\.\d+){1,3}$') }

function Get-LocalJson([string]$url, [int]$timeoutMs = 3000) {
    # мимо системного прокси (он ломает запросы к 127.0.0.1) и с жёстким таймаутом
    $r = [System.Net.HttpWebRequest]::Create($url)
    $r.Proxy = $null
    $r.Timeout = $timeoutMs
    $r.ReadWriteTimeout = $timeoutMs
    $resp = $r.GetResponse()
    try {
        $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
        return ($sr.ReadToEnd() | ConvertFrom-Json)
    } finally { $resp.Close() }
}

function Get-InstalledVersion([string]$root) {
    # 1) файл VERSION установленной версии; 2) запущенное приложение; не вышло — NaN
    foreach ($rel in @('_internal\VERSION', 'VERSION')) {
        $f = Join-Path $root $rel
        if (Test-Path -LiteralPath $f) {
            try {
                $v = ((Get-Content -LiteralPath $f -Raw -ErrorAction Stop) -as [string]).Trim().TrimStart('v', 'V')
                if (Test-VersionString $v) { return $v }
            } catch {}
        }
    }
    try {
        $v = [string](Get-LocalJson "http://127.0.0.1:$Port/api/debug/info").version
        if (Test-VersionString $v) { return $v }
    } catch {}
    return 'NaN'
}

# ---------- сеть: системный прокси (как в браузере) и повторы ----------
try {
    $sysProxy = [System.Net.WebRequest]::GetSystemWebProxy()
    $sysProxy.Credentials = [System.Net.CredentialCache]::DefaultNetworkCredentials
    [System.Net.WebRequest]::DefaultWebProxy = $sysProxy
} catch {}

function Invoke-WithRetry([scriptblock]$Action, [int]$Tries = 3) {
    for ($i = 1; $i -le $Tries; $i++) {
        try { return & $Action }
        catch {
            if ($i -eq $Tries) { throw }
            Log ("попытка {0}/{1} не удалась: {2} — повтор через 5 с" -f $i, $Tries, $_.Exception.Message) 'Yellow'
            Start-Sleep -Seconds 5
        }
    }
}

# ---------- запуск и остановка приложения ----------
function Stop-App([string]$exePath) {
    $procs = @(Get-Process -Name 'web_MVS' -ErrorAction SilentlyContinue)
    if ($procs.Count -eq 0) { Log 'приложение не запущено'; return }
    Log ("останавливаю web_MVS (процессов: {0})..." -f $procs.Count)
    $procs | Stop-Process -Force -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 40; $i++) {                      # до 20 с ждём, пока процесс исчезнет и освободит файлы
        if (@(Get-Process -Name 'web_MVS' -ErrorAction SilentlyContinue).Count -eq 0) { Start-Sleep -Milliseconds 500; Log 'приложение остановлено'; return }
        Start-Sleep -Milliseconds 500
    }
    throw 'приложение не остановилось за 20 секунд'
}

function Start-AppAndWait([string]$root, [string]$expectVersion) {
    # запускает exe и ждёт ответа сервера нужной версии; $true — поднялось
    $exe = Join-Path $root 'web_MVS.exe'
    if (-not (Test-Path -LiteralPath $exe)) { Log 'ОШИБКА: web_MVS.exe не найден' 'Red'; return $false }
    Log 'запускаю приложение...'
    Start-Process -FilePath $exe -WorkingDirectory $root | Out-Null
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if (@(Get-Process -Name 'web_MVS' -ErrorAction SilentlyContinue).Count -eq 0) {
            if ((Get-Date) -gt $deadline.AddSeconds(-80)) { Log 'процесс web_MVS завершился сразу после запуска' 'Yellow'; return $false }
        }
        try {
            $v = [string](Get-LocalJson "http://127.0.0.1:$Port/api/debug/info").version
            if ($v) {
                if ($expectVersion -and $v -ne $expectVersion) { Log "приложение отвечает, но версия $v (ждали $expectVersion)" 'Yellow'; return $false }
                Log "приложение запущено, версия $v" 'Green'
                return $true
            }
        } catch {}
    }
    Log 'приложение не ответило за 90 секунд' 'Yellow'
    return $false
}

# ---------- основной поток ----------
$tmp = Join-Path $env:TEMP ('web_mvs_upd_' + [Guid]::NewGuid().ToString('N'))
$backup = Join-Path $Root '.update_backup'
$script:swapped = $false
function Invoke-Update {
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    Write-Host ''
    Log "папка установки: $Root"
    $current = Get-InstalledVersion $Root
    Log "установлена версия: $current"

    # --- какая версия последняя ---
    $asset = $null
    $zipFile = $null
    if ($ZipPath) {
        if (-not (Test-Path -LiteralPath $ZipPath)) { throw "архив не найден: $ZipPath" }
        $zipFile = (Resolve-Path -LiteralPath $ZipPath).Path
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $za = [System.IO.Compression.ZipFile]::OpenRead($zipFile)
        try {
            $ent = $za.Entries | Where-Object { $_.FullName.Replace([string][char]92, '/') -eq '_internal/VERSION' } | Select-Object -First 1
            if (-not $ent) { throw 'в архиве нет _internal/VERSION — это не архив web_MVS' }
            $sr = New-Object System.IO.StreamReader($ent.Open())
            $latest = $sr.ReadToEnd().Trim()
            $sr.Close()
        } finally { $za.Dispose() }
        Log "версия в архиве $([IO.Path]::GetFileName($zipFile)): $latest"
    } else {
        Log 'смотрю последний релиз на GitHub...'
        try {
            $rel = Invoke-WithRetry { Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers @{ 'User-Agent' = 'web_MVS-update' } -UseBasicParsing }
            $asset = $rel.assets | Where-Object { $_.name -like 'web_MVS_*.zip' } | Select-Object -First 1
            if (-not $asset) { throw 'в последнем релизе нет файла web_MVS_*.zip' }
            $latest = ([string]$rel.tag_name).Trim().TrimStart('v', 'V')
        } catch {
            Log 'последняя версия: NaN (не удалось определить)' 'Yellow'
            Log ("причина: " + $_.Exception.Message) 'Yellow'
            Log 'GitHub недоступен с этой машины? Скачайте web_MVS_v*.zip на другом компьютере и запустите: update.ps1 -ZipPath <путь к архиву>' 'Yellow'
            throw 'не удалось узнать последнюю версию'
        }
    }
    if (-not (Test-VersionString $latest)) { $latest = 'NaN' }
    Log "последняя версия:  $latest"
    Write-Host ''

    # --- нужно ли обновляться ---
    if ($latest -ne 'NaN' -and $current -ne 'NaN') {
        $cmp = ([version]$latest).CompareTo([version]$current)
        if ($cmp -le 0 -and -not $Force) {
            if ($cmp -eq 0) { Log 'у вас уже последняя версия — обновление не нужно ✓' 'Green' }
            else { Log "у вас версия новее, чем в релизе ($current > $latest) — ничего не меняю" 'Green' }
            Log 'чтобы переустановить принудительно: update.bat -Force'
            $script:exitCode = 2
            return
        }
    }
    if ($current -eq 'NaN') { Log 'установленную версию определить не удалось (NaN) — ставлю последнюю с нуля' 'Yellow' }

    # --- скачать и проверить ---
    if (-not $zipFile) {
        $zipFile = Join-Path $tmp $asset.name
        Log ("скачиваю {0} ({1:N1} МБ)..." -f $asset.name, ($asset.size / 1MB))
        Invoke-WithRetry { Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipFile -Headers @{ 'User-Agent' = 'web_MVS-update' } -UseBasicParsing } | Out-Null
        if ($asset.size -and ((Get-Item -LiteralPath $zipFile).Length -ne [int64]$asset.size)) { throw "размер архива не совпал ($((Get-Item -LiteralPath $zipFile).Length) вместо $($asset.size))" }
        $digest = [string]$asset.digest
        if ($digest.StartsWith('sha256:')) {
            $actual = (Get-FileHash -LiteralPath $zipFile -Algorithm SHA256).Hash.ToLower()
            if ($actual -ne $digest.Substring(7).ToLower()) { throw 'контрольная сумма архива не совпала — скачивание повреждено' }
            Log 'контрольная сумма подтверждена' 'Green'
        }
    }
    $stage = Join-Path $tmp 'stage'
    Log 'распаковываю архив...'
    Expand-Archive -LiteralPath $zipFile -DestinationPath $stage -Force
    if (-not (Test-Path -LiteralPath (Join-Path $stage 'web_MVS.exe')) -or -not (Test-Path -LiteralPath (Join-Path $stage '_internal'))) { throw 'в архиве нет web_MVS.exe или _internal — архив неполный' }

    # --- остановить, заменить с откатом ---
    Stop-App (Join-Path $Root 'web_MVS.exe')
    if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    Log 'сохраняю прежнюю версию на случай отката...'
    foreach ($name in @('_internal', 'web_MVS.exe')) {
        $src = Join-Path $Root $name
        if (Test-Path -LiteralPath $src) { Move-Item -LiteralPath $src -Destination (Join-Path $backup $name) -Force }
    }
    $script:swapped = $true
    Log 'копирую новую версию...'
    # update.bat / update.ps1 не затираем посреди работы: сам скрипт обновим в самом конце
    Get-ChildItem -LiteralPath $stage -Force | Where-Object { $_.Name -notin @('update.bat', 'update.ps1') } |
        Copy-Item -Destination $Root -Recurse -Force -ErrorAction Stop
    $installed = Get-InstalledVersion $Root
    Log "файлы заменены, теперь в папке версия: $installed"

    # --- запустить и убедиться, что поднялось ---
    $up = $true
    if (-not $NoRestart) { $up = Start-AppAndWait $Root $installed }
    if (-not $up) {
        Log 'новая версия не поднялась — возвращаю прежнюю' 'Red'
        Stop-App (Join-Path $Root 'web_MVS.exe')
        Remove-Item -LiteralPath (Join-Path $Root '_internal') -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $Root 'web_MVS.exe') -Force -ErrorAction SilentlyContinue
        foreach ($name in @('_internal', 'web_MVS.exe')) {
            $b = Join-Path $backup $name
            if (Test-Path -LiteralPath $b) { Move-Item -LiteralPath $b -Destination (Join-Path $Root $name) -Force }
        }
        $script:swapped = $false
        $back = Get-InstalledVersion $Root
        Log "откат выполнен, снова версия $back"
        Start-AppAndWait $Root $back | Out-Null
        throw 'обновление не удалось, возвращена прежняя версия (подробности выше и в update.log)'
    }

    # скрипт обновления обновляем последним
    foreach ($name in @('update.ps1', 'update.bat')) {
        $s = Join-Path $stage $name
        if ((Test-Path -LiteralPath $s) -and $name -eq 'update.ps1') { try { Copy-Item -LiteralPath $s -Destination (Join-Path $Root $name) -Force } catch {} }
    }
    Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host ''
    Log "ГОТОВО: было $current  →  стало $installed" 'Green'
}

try { Invoke-Update }
catch {
    Log ("ОШИБКА: " + $_.Exception.Message) 'Red'
    if ($script:swapped) {
        # сбой посреди замены: вернуть сохранённое, чтобы приложение не осталось без файлов
        try {
            Remove-Item -LiteralPath (Join-Path $Root '_internal') -Recurse -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath (Join-Path $Root 'web_MVS.exe') -Force -ErrorAction SilentlyContinue
            foreach ($name in @('_internal', 'web_MVS.exe')) {
                $b = Join-Path $backup $name
                if (Test-Path -LiteralPath $b) { Move-Item -LiteralPath $b -Destination (Join-Path $Root $name) -Force }
            }
            Log 'прежняя версия возвращена на место' 'Yellow'
        } catch { Log ("не удалось вернуть прежнюю версию: " + $_.Exception.Message + " — копия лежит в $backup") 'Red' }
    }
    $script:exitCode = 1
}
finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    if ($Elevated) { Write-Host ''; Read-Host '[update] Нажмите Enter, чтобы закрыть окно' | Out-Null }
}
exit $script:exitCode
