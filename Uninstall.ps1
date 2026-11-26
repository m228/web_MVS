# Деинсталлятор дополнений и Python (Uninstall.bat): вернуть машину в «чистое» состояние, чтобы заново проверить установку
# через update.bat, или снять лишнее. Основное приложение (web_MVS.exe) и его данные НЕ трогает никогда.
#
# Без параметров — меню. Параметры для запуска без меню:
#   -Venvs    удалить окружения (.venv) дополнений cv_service / video_service / motion_service / level_service (код, model\, data\ остаются)
#   -Python   удалить Python 3.10–3.12, поставленный установщиком python.org (из «Программы и компоненты»; другие версии не трогаем)
#   -Addons   удалить дополнения ЦЕЛИКОМ: папки cv_service, video_service, motion_service, level_service вместе с model\, data\ (настройки камер, модели CV и уровня!)
#   -All      -Addons и -Python вместе
#   -Yes      без вопросов (для -Addons — без ввода слова подтверждения)
#   -Root <папка>  папка установки (по умолчанию — рядом со скриптом);  -NoElevate  не запрашивать права администратора (для проверок)
[CmdletBinding()]
param(
    [string]$Root = '',
    [switch]$Venvs,
    [switch]$Python,
    [switch]$Addons,
    [switch]$All,
    [switch]$Yes,
    [switch]$NoElevate,
    [switch]$Elevated
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
if ([string]::IsNullOrWhiteSpace($Root)) {
    if ($PSScriptRoot) { $Root = $PSScriptRoot }
    elseif ($PSCommandPath) { $Root = Split-Path -Parent $PSCommandPath }
    else { $Root = (Get-Location).Path }
}
$Root = (Resolve-Path -LiteralPath $Root).Path
$logFile = Join-Path $Root 'uninstall.log'
$AddonDirs = @('cv_service', 'video_service', 'motion_service', 'level_service')

function Log([string]$msg, [string]$color = '') {
    $line = "[uninstall] $msg"
    if ($color) { Write-Host $line -ForegroundColor $color } else { Write-Host $line }
    try { ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg) | Out-File -FilePath $logFile -Append -Encoding utf8 } catch {}
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
if (-not $NoElevate -and -not (Test-Admin)) {
    Write-Host '[uninstall] Нужны права администратора — запрашиваю (подтвердите окно Windows)...'
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Elevated', '-Root', "`"$Root`"")
    foreach ($sw in @('Venvs', 'Python', 'Addons', 'All', 'Yes')) { if ((Get-Variable $sw).Value) { $argList += "-$sw" } }
    try { $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs -Wait -PassThru; exit $p.ExitCode }
    catch { Write-Host '[uninstall] ОШИБКА: права администратора не получены. Запустите Uninstall.bat «от имени администратора».' -ForegroundColor Red; exit 1 }
}

function Read-Yes([string]$question) {
    if ($Yes) { return $true }
    $a = ([string](Read-Host "$question [y/N]")).Trim().ToLower()
    return $a -in @('y', 'yes', 'д', 'да')
}

function Stop-AddonProcesses {
    # процессы из .venv дополнений (и их дочерние) держат файлы: останавливаем перед удалением
    $prefixes = @($AddonDirs | ForEach-Object { (Join-Path (Join-Path $Root $_) '.venv') + '\' })
    $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $exe = $_.ExecutablePath
        $exe -and @($prefixes | Where-Object { $exe.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0 })
    foreach ($pr in $procs) {
        Get-CimInstance Win32_Process -Filter ("ParentProcessId = {0}" -f $pr.ProcessId) -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Stop-Process -Id $pr.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($procs.Count -gt 0) { Log ("остановлено процессов дополнений: {0}" -f $procs.Count); Start-Sleep -Seconds 2 }
}

function Remove-Dir([string]$path) {
    # web_MVS сам поднимает сервисы заново (сторож раз в минуту) — если папка занята, останавливаем процессы и пробуем ещё
    for ($i = 1; $i -le 3; $i++) {
        try { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop; return }
        catch {
            if ($i -eq 3) { throw }
            Stop-AddonProcesses; Start-Sleep -Seconds 2
        }
    }
}

function Remove-Venvs {
    Stop-AddonProcesses
    $n = 0
    foreach ($d in $AddonDirs) {
        $v = Join-Path (Join-Path $Root $d) '.venv'
        if (Test-Path -LiteralPath $v) { Log "удаляю окружение $d\.venv ..."; Remove-Dir $v; $n++ }
    }
    if ($n -eq 0) { Log 'окружений дополнений нет' } else { Log "удалено окружений: $n" 'Green' }
}

function Remove-AddonsWhole {
    Stop-AddonProcesses
    $n = 0
    foreach ($d in $AddonDirs) {
        $p = Join-Path $Root $d
        if (Test-Path -LiteralPath $p) { Log "удаляю $d целиком ..."; Remove-Dir $p; $n++ }
    }
    if ($n -eq 0) { Log 'дополнений рядом нет' } else { Log "удалено дополнений: $n" 'Green' }
}

function Get-PythonEntries {
    # установщик python.org (пакет «Python 3.x.y (64-bit)») записывает себя в «Программы и компоненты»; берём только 3.10–3.12
    $keys = @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
              'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')
    $out = @()
    foreach ($k in $keys) {
        if (-not (Test-Path -LiteralPath $k)) { continue }
        foreach ($e in (Get-ChildItem -LiteralPath $k -ErrorAction SilentlyContinue)) {
            $p = Get-ItemProperty -LiteralPath $e.PSPath -ErrorAction SilentlyContinue
            if ($p -and [string]$p.DisplayName -match '^Python 3\.(10|11|12)\.\d+ \(64-bit\)$' -and ($p.QuietUninstallString -or $p.UninstallString)) { $out += $p }
        }
    }
    return $out
}

function Remove-PythonInstalls {
    $entries = @(Get-PythonEntries)
    if ($entries.Count -eq 0) { Log 'установленного Python 3.10–3.12 (python.org) не найдено'; return }
    Stop-AddonProcesses
    foreach ($p in $entries) {
        $cmd = if ($p.QuietUninstallString) { [string]$p.QuietUninstallString } else { [string]$p.UninstallString + ' /quiet' }
        Log ("удаляю {0} ..." -f $p.DisplayName)
        $r = Start-Process -FilePath 'cmd.exe' -ArgumentList ('/c "' + $cmd + '"') -Wait -PassThru -WindowStyle Hidden
        if ($r.ExitCode -ne 0 -and $r.ExitCode -ne 3010) { Log ("{0}: деинсталлятор вернул код {1}" -f $p.DisplayName, $r.ExitCode) 'Yellow' }
    }
    $left = @(Get-PythonEntries)
    if ($left.Count -eq 0) { Log 'Python удалён' 'Green' } else { Log ('Python остался: ' + (($left | ForEach-Object { $_.DisplayName }) -join ', ')) 'Yellow'; $script:fail = $true }
}

function Describe-State {
    Write-Host ''
    Log "папка установки: $Root"
    foreach ($d in $AddonDirs) {
        $p = Join-Path $Root $d
        if (Test-Path -LiteralPath $p) {
            $v = if (Test-Path -LiteralPath (Join-Path $p '.venv')) { 'есть .venv' } else { 'без .venv' }
            $extra = @('model', 'data') | Where-Object { Test-Path -LiteralPath (Join-Path $p $_) }
            Log ("  {0}: {1}{2}" -f $d, $v, $(if ($extra) { '; внутри ' + ($extra -join ', ') } else { '' }))
        } else { Log "  ${d}: не установлено" }
    }
    $py = @(Get-PythonEntries)
    if ($py.Count -eq 0) { Log '  Python 3.10–3.12 (python.org): не найден' } else { foreach ($p in $py) { Log ("  {0}" -f $p.DisplayName) } }
}

$script:fail = $false
try {
    $doVenvs = $Venvs.IsPresent; $doPython = $Python.IsPresent; $doAddons = $Addons.IsPresent
    if ($All) { $doAddons = $true; $doPython = $true }
    if (-not ($doVenvs -or $doPython -or $doAddons)) {
        Describe-State
        Write-Host ''
        Write-Host 'Что удалить?' -ForegroundColor Cyan
        Write-Host '  1. Окружения дополнений (.venv)         — код, model, data остаются; потом update.bat заново создаст окружение'
        Write-Host '  2. Python 3.10–3.12 (python.org)'
        Write-Host '  3. Окружения + Python                    — как на чистой машине; дополнения ставятся заново через update.bat'
        Write-Host '  4. Дополнения целиком + Python           — ВКЛЮЧАЯ model и data (настройки камер, модели CV и уровня)!'
        Write-Host '  Q. Выход'
        switch (([string](Read-Host 'Номер')).Trim().ToLower()) {
            '1' { $doVenvs = $true }
            '2' { $doPython = $true }
            '3' { $doVenvs = $true; $doPython = $true }
            '4' { $doAddons = $true; $doPython = $true }
            default { Log 'выход без изменений'; exit 0 }
        }
    }
    if ($doAddons) {
        if (-not $Yes) {
            Write-Host 'Будут удалены папки cv_service, video_service, motion_service, level_service ЦЕЛИКОМ (включая model и data). Вернуть их нельзя.' -ForegroundColor Yellow
            if (([string](Read-Host 'Для подтверждения введите слово delete')).Trim().ToLower() -ne 'delete') { Log 'не подтверждено — дополнения не тронуты'; $doAddons = $false }
        }
        if ($doAddons) { Remove-AddonsWhole }
    } elseif ($doVenvs) {
        if (Read-Yes 'Удалить окружения (.venv) дополнений?') { Remove-Venvs } else { Log 'окружения не тронуты' }
    }
    if ($doPython) {
        $found = @(Get-PythonEntries)
        if ($found.Count -gt 0) {
            Write-Host ('Будет удалено: ' + (($found | ForEach-Object { $_.DisplayName }) -join ', ')) -ForegroundColor Yellow
            if (-not $doAddons -and -not $doVenvs) { Log 'после удаления Python окружения дополнений перестанут запускаться — update.bat создаст их заново' 'Yellow' }
        }
        if (Read-Yes 'Удалить Python?') { Remove-PythonInstalls } else { Log 'Python не тронут' }
    }
    Describe-State
} catch {
    Log ("ОШИБКА: " + $_.Exception.Message) 'Red'
    $script:fail = $true
}
finally {
    if ($Elevated) { Write-Host ''; Read-Host '[uninstall] Нажмите Enter, чтобы закрыть окно' | Out-Null }
}
if ($script:fail) { exit 1 } else { exit 0 }
