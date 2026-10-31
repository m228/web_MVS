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
#   -Components <список>  что обновлять: main (основное приложение), cv (CV микроскопа), video (видео-ролик), motion (CV RTSP / движение),
#                     через запятую или all. Без параметра — меню с выбором (прошлый выбор помнится в update_choice.json)
#   -ZipDir <папка>   папка с готовыми архивами релиза (web_MVS_v*.zip, cv_service_v*.zip, ...) вместо скачивания
#   -Yes              подтвердить тяжёлую первую установку CV (torch, несколько ГБ) без вопроса
#   -NoMenu           меню не показывать (без -Components обновляется только основное приложение, как раньше)
#   -NoBrowser        запускать приложение с --no-open: страницу в браузере оно не открывает (её перезагружает вкладка, с которой нажали «обновить»)
[CmdletBinding()]
param(
    [string]$Root = '',
    [string]$ZipPath = '',
    [string]$Repo = 'm228/web_MVS',
    [int]$Port = 8000,
    [switch]$Force,
    [switch]$NoRestart,
    [switch]$NoElevate,
    [switch]$Elevated,
    [string]$Components = '',
    [string]$ZipDir = '',
    [switch]$Yes,
    [switch]$NoMenu,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # без полосы прогресса: в Windows PowerShell 5.1 она сильно тормозит скачивание
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
    if ($Components) { $argList += @('-Components', "$Components") }
    if ($ZipDir) { $argList += @('-ZipDir', "`"$ZipDir`"") }
    if ($Yes) { $argList += '-Yes' }
    if ($NoMenu) { $argList += '-NoMenu' }
    if ($NoBrowser) { $argList += '-NoBrowser' }
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

# sha256 из метаданных релиза GitHub ('sha256:<hex>'); пусто — у релиза нет дайджеста, проверяется только размер
function Get-AssetSha($asset) {
    $d = [string]$asset.digest
    if ($d.StartsWith('sha256:')) { return $d.Substring(7) }
    return ''
}

# недокачанные архивы (*.part) лежат тут и переживают обрывы и перезапуск апдейтера: слабый интернет докачивается по частям
$script:DlDir = Join-Path $Root '.update_dl'

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

function Receive-Part([string]$url, [string]$part, [int64]$size, [int]$logEverySec, [int]$stallSec) {
    # Одна попытка скачивания: дописывает $part с того места, где он оборвался (HTTP Range). Ответ без Range (200) — пишем с начала.
    # Оборвалась связь — исключение, но всё, что успело прийти, остаётся в $part: следующая попытка продолжит оттуда.
    $have = 0
    if (Test-Path -LiteralPath $part) { $have = (Get-Item -LiteralPath $part).Length }
    $req = [System.Net.HttpWebRequest]::Create($url)
    $req.UserAgent = 'web_MVS-update'
    $req.Proxy = [System.Net.WebRequest]::DefaultWebProxy
    $req.Timeout = 60000                                  # ждать ответ сервера
    $req.ReadWriteTimeout = $stallSec * 1000              # «повисшая» связь без обрыва: чтение оборвётся, и попытка начнётся снова
    if ($have -gt 0) { $req.AddRange([int64]$have) }
    try { $resp = $req.GetResponse() }
    catch [System.Net.WebException] {
        $r = $_.Exception.Response
        if ($r -and [int]$r.StatusCode -eq 416) {          # файл на сервере не такой, как наш обрывок
            Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
            throw 'сервер не принял докачку (416) — начну заново'
        }
        throw
    }
    try {
        if ($have -gt 0) {
            $cr = [string]$resp.Headers['Content-Range']
            if ([int]$resp.StatusCode -ne 206 -or $cr -notmatch ('^bytes ' + $have + '-')) { $have = 0 }   # сервер не умеет докачку — пишем с начала
            else { Log ("докачиваю с {0:N1} МБ" -f ($have / 1MB)) }
        }
        $mode = [System.IO.FileMode]::Create
        if ($have -gt 0) { $mode = [System.IO.FileMode]::Append }
        $fs = New-Object System.IO.FileStream($part, $mode, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
        $in = $resp.GetResponseStream()
        try {
            $buf = New-Object byte[] 262144
            $total = $have; $lastLog = Get-Date
            while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
                $fs.Write($buf, 0, $n); $total += $n
                if (((Get-Date) - $lastLog).TotalSeconds -ge $logEverySec) {
                    $lastLog = Get-Date
                    if ($size -gt 0) { Log ("скачано {0:N1} из {1:N1} МБ" -f ($total / 1MB), ($size / 1MB)) } else { Log ("скачано {0:N1} МБ" -f ($total / 1MB)) }
                }
            }
        } finally { $in.Close(); $fs.Close() }
    } finally { $resp.Close() }
}

function Save-Download([string]$url, [string]$out, [int64]$size = 0, [string]$sha256 = '', [int]$logEverySec = 10, [int]$stallSec = 60, [int]$maxIdle = 15, [int]$maxPauseSec = 60) {
    # Скачивание с докачкой для слабого интернета. Недокачанный файл лежит в $script:DlDir как *.part и переживает обрывы, повторы и
    # даже новый запуск апдейтера: каждая попытка продолжает с места обрыва. Попытки, в которых хоть что-то пришло, не считаются:
    # сдаёмся только после $maxIdle подряд без единого байта (паузы 5, 10, 20, 40, 60... с). Размер и sha256 сверяются в конце;
    # не сошлась сумма — один раз качаем заново с нуля.
    New-Item -ItemType Directory -Force -Path $script:DlDir | Out-Null
    $part = Join-Path $script:DlDir ((Split-Path -Leaf $out) + '.part')
    Get-ChildItem -LiteralPath $script:DlDir -Filter '*.part' -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -ne $part } | Remove-Item -Force -ErrorAction SilentlyContinue     # обрывки от других версий не копим
    $idle = 0; $fresh = $false
    while ($true) {
        $have = 0
        if (Test-Path -LiteralPath $part) { $have = (Get-Item -LiteralPath $part).Length }
        if ($size -gt 0 -and $have -gt $size) { Remove-Item -LiteralPath $part -Force; $have = 0 }
        $err = ''
        if ($size -le 0 -or $have -lt $size) {
            try { Receive-Part $url $part $size $logEverySec $stallSec } catch { $err = $_.Exception.Message }
        }
        $now = 0
        if (Test-Path -LiteralPath $part) { $now = (Get-Item -LiteralPath $part).Length }
        $complete = if ($size -gt 0) { $now -eq $size } else { -not $err -and $now -gt 0 }
        if ($complete) {
            if ($sha256) {
                $actual = (Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash.ToLower()
                if ($actual -ne $sha256.ToLower()) {
                    Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
                    if ($fresh) { throw 'контрольная сумма архива не совпала — скачивание повреждено' }
                    $fresh = $true
                    Log 'контрольная сумма не совпала — качаю заново с нуля' 'Yellow'
                    continue
                }
                Log 'контрольная сумма подтверждена' 'Green'
            }
            Move-Item -LiteralPath $part -Destination $out -Force
            if (-not (Get-ChildItem -LiteralPath $script:DlDir -Force -ErrorAction SilentlyContinue)) { Remove-Item -LiteralPath $script:DlDir -Force -ErrorAction SilentlyContinue }
            return
        }
        if ($now -gt $have) { $idle = 0 } else { $idle++ }
        $szText = ''
        if ($size -gt 0) { $szText = " из {0:N1}" -f ($size / 1MB) }
        if ($idle -ge $maxIdle) {
            throw ("не удалось докачать: {0} попыток подряд без данных ({1}). Скачано {2:N1}{3} МБ сохранено в {4} — следующий запуск продолжит" -f $idle, $err, ($now / 1MB), $szText, $script:DlDir)
        }
        $pause = [Math]::Min($maxPauseSec, 5 * [Math]::Pow(2, [Math]::Max(0, $idle - 1)))
        if (-not $err) { $err = 'соединение закрыто раньше времени' }
        Log ("связь оборвалась: {0} — скачано {1:N1}{2} МБ, продолжу с этого места через {3} с" -f $err, ($now / 1MB), $szText, $pause) 'Yellow'
        Start-Sleep -Seconds $pause
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
    if ($NoBrowser) { Start-Process -FilePath $exe -WorkingDirectory $root -ArgumentList '--no-open' | Out-Null }
    else { Start-Process -FilePath $exe -WorkingDirectory $root | Out-Null }
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
        Save-Download $asset.browser_download_url $zipFile ([int64]$asset.size) (Get-AssetSha $asset)
        if ($asset.size -and ((Get-Item -LiteralPath $zipFile).Length -ne [int64]$asset.size)) { throw "размер архива не совпал ($((Get-Item -LiteralPath $zipFile).Length) вместо $($asset.size))" }
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

# ---------- компоненты: основное приложение + дополнения (сервисы рядом) ----------
# Один релиз GitHub несёт все архивы одной версии: web_MVS_v*.zip, cv_service_v*.zip, video_service_v*.zip, motion_service_v*.zip.
# Что обновлять — выбирает меню (или -Components main,cv,video,motion); прошлый выбор помнится в update_choice.json.
$ComponentDefs = @(
    @{ Key = 'main';   Title = 'Основное приложение (web_MVS)';           Dir = '';               Asset = 'web_MVS_*.zip';       Marker = '';                Heavy = $false },
    @{ Key = 'cv';     Title = 'CV микроскопа (cv_service)';               Dir = 'cv_service';     Asset = 'cv_service_*.zip';    Marker = 'detector.py';     Heavy = $true },
    @{ Key = 'video';  Title = 'Видео-ролик микроскопа (video_service)';   Dir = 'video_service';  Asset = 'video_service_*.zip'; Marker = 'render.py';       Heavy = $false },
    @{ Key = 'motion'; Title = 'CV RTSP / движение (motion_service)';      Dir = 'motion_service'; Asset = 'motion_service_*.zip'; Marker = 'motion_engine.py'; Heavy = $false }
)
$script:addonResults = New-Object System.Collections.ArrayList
$script:relCache = $null
$script:relTried = $false

function Get-ComponentDef([string]$key) { return ($ComponentDefs | Where-Object { $_.Key -eq $key } | Select-Object -First 1) }
function Test-AddonInstalled($d) { return (Test-Path -LiteralPath (Join-Path (Join-Path $Root $d.Dir) $d.Marker)) }
function Get-AddonVersion($d) {
    $f = Join-Path (Join-Path $Root $d.Dir) 'VERSION'
    if (Test-Path -LiteralPath $f) {
        try { $v = ((Get-Content -LiteralPath $f -Raw -ErrorAction Stop) -as [string]).Trim().TrimStart('v', 'V'); if (Test-VersionString $v) { return $v } } catch {}
    }
    return 'NaN'
}

function Get-Release {
    # последний релиз GitHub (один запрос на запуск); $null — недоступен
    if ($script:relTried) { return $script:relCache }
    $script:relTried = $true
    try { $script:relCache = Invoke-WithRetry { Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers @{ 'User-Agent' = 'web_MVS-update' } -UseBasicParsing } }
    catch { $script:relCache = $null; Log ("релиз GitHub недоступен: " + $_.Exception.Message) 'Yellow' }
    return $script:relCache
}

function Get-ZipDirVersion([string]$zip) {
    # версия внутри архива: у основного приложения _internal/VERSION, у дополнений VERSION
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $za = [System.IO.Compression.ZipFile]::OpenRead($zip)
    try {
        $ent = $za.Entries | Where-Object { $n = $_.FullName.Replace([string][char]92, '/'); $n -eq 'VERSION' -or $n -eq '_internal/VERSION' } | Select-Object -First 1
        if (-not $ent) { return 'NaN' }
        $sr = New-Object System.IO.StreamReader($ent.Open()); try { $v = $sr.ReadToEnd().Trim().TrimStart('v', 'V') } finally { $sr.Close() }
        if (Test-VersionString $v) { return $v } else { return 'NaN' }
    } finally { $za.Dispose() }
}

function Find-ZipInDir($d) {
    if (-not $ZipDir -or -not (Test-Path -LiteralPath $ZipDir)) { return $null }
    $pat = $d.Asset -replace '_\*', '_v*'
    return (Get-ChildItem -LiteralPath $ZipDir -Filter $pat -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1)
}

function Get-LatestVersionFor($d) {
    # что доступно: из папки с архивами (-ZipDir) или из релиза GitHub; 'NaN', если неизвестно
    $z = Find-ZipInDir $d
    if ($z) { return (Get-ZipDirVersion $z.FullName) }
    if ($ZipDir) { return 'NaN' }
    $rel = Get-Release
    if ($rel) { $v = ([string]$rel.tag_name).Trim().TrimStart('v', 'V'); if (Test-VersionString $v) { return $v } }
    return 'NaN'
}

function Get-ChoiceFile { return (Join-Path $Root 'update_choice.json') }
function Read-Choice {
    try { return @((Get-Content -LiteralPath (Get-ChoiceFile) -Raw -Encoding UTF8 | ConvertFrom-Json).components) } catch { return @() }
}
function Save-Choice($keys) {
    try { (@{ components = @($keys) } | ConvertTo-Json -Compress) | Out-File -FilePath (Get-ChoiceFile) -Encoding utf8 } catch {}
}

function Read-ComponentList([string]$text) {
    # "main,cv" / "main cv" / "all" -> список ключей (только известные)
    $keys = @()
    foreach ($t in ($text -split '[,;\s]+')) {
        $t = $t.Trim().ToLower()
        if (-not $t) { continue }
        if ($t -eq 'all') { return @($ComponentDefs | ForEach-Object { $_.Key }) }
        if (Get-ComponentDef $t) { $keys += $t } else { throw "неизвестный компонент '$t' (допустимо: main, cv, video, motion, all)" }
    }
    return @($keys | Select-Object -Unique)
}

function Show-Menu {
    # интерактивный выбор; возвращает список ключей или $null (выход)
    $remembered = @(Read-Choice | Where-Object { Get-ComponentDef $_ })
    if ($remembered.Count -gt 0) { $sel = $remembered }
    else { $sel = @('main') + @($ComponentDefs | Where-Object { $_.Key -ne 'main' -and (Test-AddonInstalled $_) } | ForEach-Object { $_.Key }) }
    Write-Host ''
    Log 'смотрю версии (GitHub)...'
    $rows = @()
    $i = 0
    foreach ($d in $ComponentDefs) {
        $i++
        if ($d.Key -eq 'main') { $cur = Get-InstalledVersion $Root; $have = $true }
        else { $have = Test-AddonInstalled $d; $cur = if ($have) { Get-AddonVersion $d } else { 'не установлено' } }
        $rows += [pscustomobject]@{ N = $i; Def = $d; Cur = $cur; Latest = (Get-LatestVersionFor $d) }
    }
    while ($true) {
        Write-Host ''
        Write-Host 'Что обновить?' -ForegroundColor Cyan
        foreach ($r in $rows) {
            $mark = if ($sel -contains $r.Def.Key) { 'x' } else { ' ' }
            Write-Host ("  [{0}] {1}. {2,-46} {3} → {4}" -f $mark, $r.N, $r.Def.Title, $r.Cur, $r.Latest)
        }
        Write-Host ''
        $ans = Read-Host 'Номера через пробел (напр. 1 4), A — всё, Enter — как отмечено, Q — выход'
        $a = ([string]$ans).Trim().ToLower()
        if ($a -eq '') { if ($sel.Count -eq 0) { Write-Host 'Ничего не отмечено.' -ForegroundColor Yellow; continue } return $sel }
        if ($a -eq 'q') { return $null }
        if ($a -eq 'a') { $sel = @($ComponentDefs | ForEach-Object { $_.Key }); continue }
        $picked = @()
        $bad = $false
        foreach ($t in ($a -split '[,;\s]+')) {
            if (-not $t) { continue }
            if ($t -match '^\d+$' -and [int]$t -ge 1 -and [int]$t -le $rows.Count) { $picked += $rows[[int]$t - 1].Def.Key } else { $bad = $true }
        }
        if ($bad -or $picked.Count -eq 0) { Write-Host 'Не понял. Пример: 1 4' -ForegroundColor Yellow; continue }
        $sel = @($picked | Select-Object -Unique)
    }
}

function Stop-Addon([string]$dir) {
    # останавливает сервис дополнения (процессы из его .venv и их дочерние) — файлы можно заменять;
    # web_MVS (сторож services.py) сам поднимет сервис заново в течение минуты, а при перезапуске приложения — сразу
    $venv = (Join-Path $dir '.venv') + '\'
    $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($venv, [StringComparison]::OrdinalIgnoreCase) })
    if ($procs.Count -eq 0) { return }
    Log ("останавливаю сервис в {0} (процессов: {1})..." -f (Split-Path -Leaf $dir), $procs.Count)
    foreach ($pr in $procs) {
        Get-CimInstance Win32_Process -Filter ("ParentProcessId = {0}" -f $pr.ProcessId) -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Stop-Process -Id $pr.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

function Add-AddonResult([string]$key, [string]$status, [string]$text) {
    [void]$script:addonResults.Add([pscustomobject]@{ Key = $key; Status = $status; Text = $text })
}

function Update-Addon($d) {
    $dir = Join-Path $Root $d.Dir
    $title = $d.Title
    Write-Host ''
    Log "=== $title ==="
    try {
        $installed = Test-AddonInstalled $d
        $cur = if ($installed) { Get-AddonVersion $d } else { 'не установлено' }
        Log "установлено: $cur"

        # --- источник: папка с архивами или релиз GitHub ---
        $zip = $null; $asset = $null; $latest = 'NaN'
        $zf = Find-ZipInDir $d
        if ($zf) { $zip = $zf.FullName; $latest = Get-ZipDirVersion $zip; Log "архив: $($zf.Name) (версия $latest)" }
        elseif ($ZipDir) { throw "в папке $ZipDir нет архива $($d.Asset -replace '_\*', '_v*')" }
        else {
            $rel = Get-Release
            if (-not $rel) { throw 'не удалось получить релиз GitHub (нет связи?). Скачайте архивы на другом компьютере и запустите с -ZipDir <папка>' }
            $asset = $rel.assets | Where-Object { $_.name -like $d.Asset } | Select-Object -First 1
            if (-not $asset) { throw "в последнем релизе нет файла $($d.Asset)" }
            $latest = ([string]$rel.tag_name).Trim().TrimStart('v', 'V')
        }
        Log "доступно: $latest"
        if ($installed -and (Test-VersionString $latest) -and (Test-VersionString $cur) -and -not $Force) {
            if (([version]$latest).CompareTo([version]$cur) -le 0) {
                Log 'уже последняя версия ✓' 'Green'
                Add-AddonResult $d.Key 'ok' "$cur (без изменений)"
                return
            }
        }

        # --- тяжёлая первая установка — только с подтверждением ---
        if (-not $installed -and $d.Heavy -and -not $Yes) {
            if ($NoMenu) { throw 'первая установка тяжёлая (несколько ГБ: torch) — подтвердите её (галочка/ключ -Yes)' }
            $ans = Read-Host 'Первая установка CV тянет несколько ГБ (torch, ultralytics) и занимает время. Установить сейчас? [y/N]'
            if ($ans.Trim().ToLower() -notin @('y', 'yes', 'д', 'да')) { Log 'пропускаю по вашему решению'; Add-AddonResult $d.Key 'skip' 'пропущено'; return }
        }

        # --- скачать и проверить ---
        if (-not $zip) {
            $zip = Join-Path $tmp $asset.name
            Log ("скачиваю {0} ({1:N1} МБ)..." -f $asset.name, ($asset.size / 1MB))
            Save-Download $asset.browser_download_url $zip ([int64]$asset.size) (Get-AssetSha $asset)
            if ($asset.size -and ((Get-Item -LiteralPath $zip).Length -ne [int64]$asset.size)) { throw 'размер архива не совпал — скачивание повреждено' }
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $za = [System.IO.Compression.ZipFile]::OpenRead($zip)
        try { $ok = @($za.Entries | Where-Object { $_.FullName.Replace([string][char]92, '/') -eq $d.Marker }).Count -gt 0 } finally { $za.Dispose() }
        if (-not $ok) { throw "в архиве нет $($d.Marker) — это не архив $($d.Dir)" }

        $stage = Join-Path $tmp ('stage_' + $d.Key)
        Expand-Archive -LiteralPath $zip -DestinationPath $stage -Force
        $reqOld = if (Test-Path -LiteralPath (Join-Path $dir 'requirements.txt')) { (Get-FileHash -LiteralPath (Join-Path $dir 'requirements.txt')).Hash } else { '' }

        # --- остановить сервис, заменить с откатом (.venv, data, model, video в архиве нет — не затрагиваются) ---
        Stop-Addon $dir
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $bk = Join-Path (Join-Path $Root '.update_backup') $d.Dir
        if (Test-Path -LiteralPath $bk) { Remove-Item -LiteralPath $bk -Recurse -Force -ErrorAction SilentlyContinue }
        New-Item -ItemType Directory -Force -Path $bk | Out-Null
        $names = @(Get-ChildItem -LiteralPath $stage -Force | ForEach-Object { $_.Name })
        foreach ($n in $names) { $s = Join-Path $dir $n; if (Test-Path -LiteralPath $s) { Copy-Item -LiteralPath $s -Destination $bk -Recurse -Force } }
        try {
            Get-ChildItem -LiteralPath $stage -Force | Copy-Item -Destination $dir -Recurse -Force -ErrorAction Stop
        } catch {
            Log ("ошибка копирования: " + $_.Exception.Message + " — возвращаю прежние файлы") 'Red'
            Get-ChildItem -LiteralPath $bk -Force | Copy-Item -Destination $dir -Recurse -Force -ErrorAction SilentlyContinue
            throw
        }
        Remove-Item -LiteralPath $bk -Recurse -Force -ErrorAction SilentlyContinue
        $now = Get-AddonVersion $d
        Log "файлы заменены, версия в папке: $now" 'Green'

        # --- окружение (venv): первый раз — setup.bat; потом — только если изменился requirements.txt ---
        $py = Join-Path $dir '.venv\Scripts\python.exe'
        $reqNew = if (Test-Path -LiteralPath (Join-Path $dir 'requirements.txt')) { (Get-FileHash -LiteralPath (Join-Path $dir 'requirements.txt')).Hash } else { '' }
        if (-not (Test-Path -LiteralPath $py)) {
            Log 'первая установка: создаю окружение и ставлю зависимости (setup.bat) — это может занять время...' 'Cyan'
            $nul = Join-Path $tmp 'empty_stdin.txt'; New-Item -ItemType File -Force -Path $nul | Out-Null
            # stdin из пустого файла: pause в конце setup.bat не ждёт нажатия клавиши; внешние кавычки снимет сам cmd /c
            $cmdLine = '/c ""' + (Join-Path $dir 'setup.bat') + '" < "' + $nul + '""'
            $pr = Start-Process -FilePath 'cmd.exe' -ArgumentList $cmdLine -WorkingDirectory $dir -Wait -PassThru -NoNewWindow
            if (-not (Test-Path -LiteralPath $py)) { throw "setup.bat не создал окружение (код $($pr.ExitCode)); запустите $($d.Dir)\setup.bat вручную и посмотрите ошибку" }
            Log 'окружение готово' 'Green'
        } elseif ($reqOld -ne $reqNew) {
            if ($d.Heavy) { Log "requirements.txt изменился — запустите $($d.Dir)\setup.bat вручную (там тяжёлые пакеты)" 'Yellow' }
            else {
                Log 'зависимости изменились — обновляю пакеты...' 'Cyan'
                & $py -m pip install -q -r (Join-Path $dir 'requirements.txt')
                if ($LASTEXITCODE -ne 0) { throw 'pip не смог обновить зависимости (смотрите вывод выше)' }
            }
        }
        Add-AddonResult $d.Key 'updated' "$cur → $now"
    } catch {
        Log ("ОШИБКА ($title): " + $_.Exception.Message) 'Red'
        Add-AddonResult $d.Key 'fail' $_.Exception.Message
        $script:exitCode = 1
    }
}

function Invoke-All {
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    # 1) что обновлять
    $sel = @()
    if ($Components) { $sel = Read-ComponentList $Components }
    elseif ($ZipPath) { $sel = @('main') }                       # прежнее поведение: архив основного приложения
    elseif ($NoMenu -or -not [Environment]::UserInteractive) { $sel = @('main') }
    else {
        $sel = Show-Menu
        if ($null -eq $sel) { Log 'выход без изменений'; $script:exitCode = 0; return }
    }
    if ($sel.Count -eq 0) { Log 'не выбрано ни одного компонента'; return }
    Save-Choice $sel
    Log ("обновляю: " + (($sel | ForEach-Object { (Get-ComponentDef $_).Key }) -join ', '))

    # 2) основное приложение из папки с архивами (-ZipDir), если архив не указан явно
    if (($sel -contains 'main') -and -not $ZipPath -and $ZipDir) {
        $mz = Find-ZipInDir (Get-ComponentDef 'main')
        if ($mz) { $script:ZipPath = $mz.FullName; Log "основное приложение из папки: $($mz.Name)" }
    }

    # 3) сначала дополнения (их сервисы остановятся и сразу поднимутся новыми при запуске обновлённого приложения), потом основное
    foreach ($d in $ComponentDefs) { if ($d.Key -ne 'main' -and ($sel -contains $d.Key)) { Update-Addon $d } }
    $mainResult = $null
    if ($sel -contains 'main') {
        $before = $script:exitCode
        Invoke-Update
        $mainResult = if ($script:exitCode -eq 2) { 'без изменений' } else { 'обновлено' }
    }

    # 4) итог
    Write-Host ''
    Log '--- итог ---'
    if ($mainResult) { Log "основное приложение: $mainResult" }
    foreach ($r in $script:addonResults) { $c = switch ($r.Status) { 'updated' { 'Green' } 'fail' { 'Red' } default { '' } }; Log ("{0}: {1}" -f (Get-ComponentDef $r.Key).Title, $r.Text) $c }
    $changed = @($script:addonResults | Where-Object { $_.Status -eq 'updated' }).Count
    if ($script:exitCode -eq 2 -and $changed -gt 0) { $script:exitCode = 0 }
    if ($changed -gt 0 -and -not ($sel -contains 'main')) { Log 'сервисы дополнений поднимет web_MVS (в течение минуты); если приложение закрыто — запустите его' }
}

try { Invoke-All }
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
