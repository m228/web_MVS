"""Сетевые операции для приёма GigE Vision — то, что делают утилиты MVS
(EnabledJumboPacket.exe / GigEVisionDriverTool.exe), но прямо из приложения,
чтобы не открывать MVS.

- status(): только чтение — список адаптеров, их IP, статус jumbo-кадров и
  привязан ли фильтр-драйвер GigE (neugev*). Не требует прав администратора.
- enable_jumbo(name) / enable_filter(name): включают jumbo-кадры и фильтр-драйвер.
  Меняют системные сетевые настройки -> ТРЕБУЮТ запуска приложения от имени
  администратора.

ВАЖНО: имя свойства jumbo локализовано ("Большой кадр" на русской Windows),
поэтому работаем через RegistryKeyword '*JumboPacket' (не зависит от языка).
Вывод PowerShell принудительно в UTF-8, иначе кириллица превращается в кракозябры.
"""
import json
import subprocess


def _ps(command, timeout=40):
    # форсируем UTF-8 на выводе PowerShell, читаем как UTF-8
    full = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $ErrorActionPreference='Stop'; " + command
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", full],
            capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
    except FileNotFoundError:
        return 1, "", "powershell не найден (не Windows?)"
    except subprocess.TimeoutExpired:
        return 1, "", f"PowerShell превысил таймаут ({timeout} c)"
    except Exception as exc:
        return 1, "", str(exc)


# статус администратора неизменен за время жизни процесса — кэшируем, чтобы не
# плодить процесс PowerShell на каждый вызов (is_admin зовётся как guard в каждом
# действии и ещё раз внутри status())
_admin_cache = None


def is_admin():
    global _admin_cache
    if _admin_cache is None:
        code, out, _ = _ps(
            "[bool]([Security.Principal.WindowsPrincipal]"
            "[Security.Principal.WindowsIdentity]::GetCurrent())"
            ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)"
        )
        _admin_cache = out.strip().lower().startswith("true")
    return _admin_cache


# фильтр-драйвер GigE Vision от MVS опознаём по ComponentID/имени
_FILTER_MATCH = r"neugev|GigE\s*Vision|Hikrobot|MVS"

# присваивание $match как single-quoted литерала PS — вставляем ОДНОЙ управляемой
# строкой в начало скрипта вместо текстовой подстановки regex в середину кода
_MATCH_ASSIGN = "$match = '" + _FILTER_MATCH.replace("'", "''") + "'\n"


def _ps_adapter(name):
    """Проверить имя адаптера и экранировать для вставки внутрь '...' PowerShell.

    Возвращает экранированное имя либо None, если имя пустое или содержит
    управляющие символы (перевод строки/возврат каретки/NUL размыли бы скрипт).
    Имена адаптеров Windows могут содержать пробелы, '*', '(', ')', '#' — их не
    режем, полагаемся на удвоение кавычек в single-quoted строке PS.
    """
    if not name or not name.strip():
        return None
    if any(c in name for c in "\r\n\x00"):
        return None
    return name.replace("'", "''")


def status():
    ps = _MATCH_ASSIGN + r"""
$ErrorActionPreference='SilentlyContinue'
$out=@()
foreach($a in (Get-NetAdapter)){
  $ip=((Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress) -join ', '
  $jp=Get-NetAdapterAdvancedProperty -Name $a.Name -RegistryKeyword '*JumboPacket' -ErrorAction SilentlyContinue | Select-Object -First 1
  $jumbo=if($jp){[string]$jp.RegistryValue}else{'нет'}
  $bind=Get-NetAdapterBinding -Name $a.Name -ErrorAction SilentlyContinue | Where-Object { $_.ComponentID -match $match -or $_.DisplayName -match $match } | Select-Object -First 1
  $out+=[pscustomobject]@{
    name=$a.Name; status=[string]$a.Status; ip=$ip;
    jumbo=[string]$jumbo;
    filter_present=[bool]($bind -ne $null);
    filter_enabled=[bool]($bind -ne $null -and $bind.Enabled)
  }
}
$out | ConvertTo-Json -Compress
"""

    code, out, err = _ps(ps)
    adapters = []
    if out:
        try:
            data = json.loads(out)
            adapters = data if isinstance(data, list) else [data]
        except Exception:
            adapters = []
    return {"admin": is_admin(), "adapters": adapters, "error": err or None}


def enable_jumbo(name):
    if not is_admin():
        return {"ok": False, "error": "нужны права администратора (запустите приложение от имени администратора)"}
    safe = _ps_adapter(name)
    if safe is None:
        return {"ok": False, "error": "не указано корректное имя адаптера"}
    # работаем через RegistryKeyword/RegistryValue — не зависит от языка Windows
    ps = r"""
$p=Get-NetAdapterAdvancedProperty -Name '%NAME%' -RegistryKeyword '*JumboPacket'
if(-not $p){ throw 'у адаптера нет настройки Jumbo (RegistryKeyword *JumboPacket)' }
$best=$p.ValidRegistryValues | Where-Object {$_ -match '^\d+$'} | Sort-Object {[int64]$_} | Select-Object -Last 1
if(-not $best){ throw 'нет числовых значений Jumbo' }
Set-NetAdapterAdvancedProperty -Name '%NAME%' -RegistryKeyword '*JumboPacket' -RegistryValue $best
"$best"
""".replace("%NAME%", safe)
    code, out, err = _ps(ps)
    if code != 0:
        return {"ok": False, "error": err or "не удалось включить jumbo"}
    return {"ok": True, "jumbo": out.strip()}


def disable_jumbo(name):
    if not is_admin():
        return {"ok": False, "error": "нужны права администратора"}
    safe = _ps_adapter(name)
    if safe is None:
        return {"ok": False, "error": "не указано корректное имя адаптера"}
    # ставим минимальное (отключённое) значение jumbo
    ps = r"""
$p=Get-NetAdapterAdvancedProperty -Name '%NAME%' -RegistryKeyword '*JumboPacket'
if(-not $p){ throw 'у адаптера нет настройки Jumbo' }
$min=$p.ValidRegistryValues | Where-Object {$_ -match '^\d+$'} | Sort-Object {[int64]$_} | Select-Object -First 1
if(-not $min){ throw 'нет числовых значений Jumbo' }
Set-NetAdapterAdvancedProperty -Name '%NAME%' -RegistryKeyword '*JumboPacket' -RegistryValue $min
"$min"
""".replace("%NAME%", safe)
    code, out, err = _ps(ps)
    if code != 0:
        return {"ok": False, "error": err or "не удалось выключить jumbo"}
    return {"ok": True, "jumbo": out.strip()}


def enable_filter(name):
    if not is_admin():
        return {"ok": False, "error": "нужны права администратора (запустите приложение от имени администратора)"}
    safe = _ps_adapter(name)
    if safe is None:
        return {"ok": False, "error": "не указано корректное имя адаптера"}
    ps = _MATCH_ASSIGN + r"""
$b=Get-NetAdapterBinding -Name '%NAME%' | Where-Object { $_.ComponentID -match $match -or $_.DisplayName -match $match } | Select-Object -First 1
if(-not $b){ throw 'фильтр-драйвер GigE (neugev) не найден среди привязок адаптера — установлен ли он?' }
Enable-NetAdapterBinding -Name '%NAME%' -ComponentID $b.ComponentID
$b.ComponentID
""".replace("%NAME%", safe)
    code, out, err = _ps(ps)
    if code != 0:
        return {"ok": False, "error": err or "не удалось включить фильтр-драйвер"}
    return {"ok": True, "component": out.strip()}


def disable_filter(name):
    if not is_admin():
        return {"ok": False, "error": "нужны права администратора"}
    safe = _ps_adapter(name)
    if safe is None:
        return {"ok": False, "error": "не указано корректное имя адаптера"}
    ps = _MATCH_ASSIGN + r"""
$b=Get-NetAdapterBinding -Name '%NAME%' | Where-Object { $_.ComponentID -match $match -or $_.DisplayName -match $match } | Select-Object -First 1
if(-not $b){ throw 'фильтр-драйвер GigE не найден среди привязок адаптера' }
Disable-NetAdapterBinding -Name '%NAME%' -ComponentID $b.ComponentID
$b.ComponentID
""".replace("%NAME%", safe)
    code, out, err = _ps(ps)
    if code != 0:
        return {"ok": False, "error": err or "не удалось выключить фильтр-драйвер"}
    return {"ok": True, "component": out.strip()}
