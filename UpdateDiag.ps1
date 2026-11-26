# Диагностика «после обновления приложение пропало»: собирает в один файл всё, что известно о состоянии и падениях web_MVS.
# Ничего не меняет, только читает. Отчёт: update_diag_<дата-время>.txt рядом со скриптом (и печатается в консоль).
# Запуск: UpdateDiag.bat (двойной клик) или  powershell -ExecutionPolicy Bypass -File UpdateDiag.ps1 [-Port 8000]
param([int]$Port = 8000)
$ErrorActionPreference = 'Continue'
$root = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$out = Join-Path $root ("update_diag_{0}.txt" -f (Get-Date -Format 'yyyyMMdd_HHmm'))
$r = New-Object System.Collections.Generic.List[string]
function Add([string]$s) { $r.Add($s) }
function Head([string]$s) { Add ''; Add "== $s ==" }

Add ("Диагностика обновления · {0} · ПК {1}" -f (Get-Date -Format 'dd.MM.yyyy HH:mm:ss'), $env:COMPUTERNAME)
Add ("папка: $root")

Head 'версия exe и ОС'
try { Add ("web_MVS.exe: изменён {0}" -f (Get-Item (Join-Path $root 'web_MVS.exe') -ErrorAction Stop).LastWriteTime) } catch { Add 'web_MVS.exe рядом со скриптом не найден' }
try { Add ("Windows: " + (Get-CimInstance Win32_OperatingSystem).Caption + ' ' + [Environment]::OSVersion.Version) } catch {}

Head 'процессы'
$p = @(Get-Process web_MVS -ErrorAction SilentlyContinue)
if ($p.Count) { foreach ($x in $p) { Add ("web_MVS PID {0}  запущен {1}  {2}" -f $x.Id, $x.StartTime, $x.Path) } } else { Add 'процесса web_MVS НЕТ' }
foreach ($x in @(Get-Process python*, pythonw* -ErrorAction SilentlyContinue)) { Add ("{0} PID {1}  запущен {2}  {3}" -f $x.ProcessName, $x.Id, $x.StartTime, $x.Path) }

Head 'ответ сервера'
[System.Net.WebRequest]::DefaultWebProxy = $null
try { Add ((Invoke-WebRequest "http://127.0.0.1:$Port/api/version" -UseBasicParsing -TimeoutSec 5).Content) } catch { Add ('НЕ ОТВЕЧАЕТ: ' + $_.Exception.Message) }

Head 'update.log (последние 80 строк)'
$ul = Join-Path $root 'update.log'
if (Test-Path -LiteralPath $ul) { foreach ($l in (Get-Content -LiteralPath $ul -Tail 80 -Encoding UTF8)) { Add $l } } else { Add 'update.log не найден' }

Head 'logs\app.log и logs\crash.log (журнал приложения, есть с версии с console_guard)'
foreach ($n in 'app.log', 'crash.log') {
    $f = Join-Path $root "logs\$n"
    if (Test-Path -LiteralPath $f) { Add "-- $n (последние 40 строк)"; foreach ($l in (Get-Content -LiteralPath $f -Tail 40 -Encoding UTF8)) { Add $l } } else { Add "-- $n нет" }
}

Head 'Windows: падения и зависания web_MVS за 7 дней'
$ev = @()
try { $ev = @(Get-WinEvent -FilterHashtable @{ LogName = 'Application'; StartTime = (Get-Date).AddDays(-7) } -ErrorAction Stop | Where-Object { $_.Message -match 'web_MVS' -and $_.Id -in 1000, 1002 } | Select-Object -First 12) } catch {}
if ($ev.Count) {
    foreach ($e in $ev) { Add ("{0}  id={1}  {2}" -f $e.TimeCreated, $e.Id, $e.ProviderName); foreach ($l in (($e.Message -split "`r?`n") | Where-Object { $_ } | Select-Object -First 8)) { Add ('    ' + $l) } }
} else { Add 'записей нет' }

Head 'Windows Error Reporting: загруженные библиотеки в момент последнего падения'
try {
    $dirs = @()
    foreach ($base in "$env:ProgramData\Microsoft\Windows\WER\ReportArchive", "$env:ProgramData\Microsoft\Windows\WER\ReportQueue") {
        if (Test-Path $base) { $dirs += Get-ChildItem $base -Directory -Filter 'AppCrash_web_MVS*' -ErrorAction SilentlyContinue }
    }
    $dirs = @($dirs | Sort-Object LastWriteTime -Descending | Select-Object -First 1)
    if ($dirs.Count) {
        $rep = Join-Path $dirs[0].FullName 'Report.wer'
        Add ("отчёт: {0}  ({1})" -f $rep, $dirs[0].LastWriteTime)
        $txt = Get-Content -LiteralPath $rep -Encoding Unicode -ErrorAction Stop
        foreach ($l in ($txt | Where-Object { $_ -match '^Sig\[' })) { Add ('    ' + $l) }
        foreach ($l in ($txt | Where-Object { $_ -match '^LoadedModule\[' -and $_ -notmatch '(?i)=C:\\Windows\\' })) { Add ('    ' + $l) }
    } else { Add 'отчётов AppCrash_web_MVS нет (Windows мог их не сохранить)' }
} catch { Add ('не прочитал: ' + $_.Exception.Message) }

$r | Out-File -FilePath $out -Encoding utf8
$r | ForEach-Object { Write-Host $_ }
Write-Host ''
Write-Host ("Отчёт сохранён: " + $out) -ForegroundColor Green
