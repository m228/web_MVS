"""Сетевые операции для приёма GigE Vision — то, что делают утилиты MVS
(EnabledJumboPacket.exe / GigEVisionDriverTool.exe), но прямо из приложения,
чтобы не открывать MVS.

- status(): только чтение — список адаптеров, их IP, статус jumbo-кадров и
  привязан ли фильтр-драйвер GigE (neugev*). Не требует прав администратора.
- enable_jumbo(name) / enable_filter(name): включают jumbo-кадры и фильтр-драйвер.
  Меняют системные сетевые настройки -> ТРЕБУЮТ запуска приложения от имени
  администратора. Если прав нет — возвращают подсказку.

Всё через PowerShell (Get/Set-NetAdapter*). На не-Windows вернётся ошибка.
"""
import json
import subprocess


def _ps(command, timeout=40):
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
    except FileNotFoundError:
        return 1, "", "powershell не найден (не Windows?)"
    except Exception as exc:
        return 1, "", str(exc)


def is_admin():
    code, out, _ = _ps(
        "[bool]([Security.Principal.WindowsPrincipal]"
        "[Security.Principal.WindowsIdentity]::GetCurrent())"
        ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)"
    )
    return out.strip().lower().startswith("true")


# фильтр-драйвер GigE Vision от MVS опознаём по ComponentID/имени
_FILTER_MATCH = r"neugev|GigE\s*Vision|Hikrobot|MVS"


def status():
    ps = r"""
$ErrorActionPreference='SilentlyContinue'
$out=@()
foreach($a in (Get-NetAdapter)){
  $ip=((Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress) -join ', '
  $jp=Get-NetAdapterAdvancedProperty -Name $a.Name -DisplayName '*Jumbo*' -ErrorAction SilentlyContinue | Select-Object -First 1
  $jumbo=if($jp){$jp.DisplayValue}else{'нет'}
  $bind=Get-NetAdapterBinding -Name $a.Name -ErrorAction SilentlyContinue | Where-Object { $_.ComponentID -match '%MATCH%' -or $_.DisplayName -match '%MATCH%' } | Select-Object -First 1
  $out+=[pscustomobject]@{
    name=$a.Name; status=[string]$a.Status; ip=$ip;
    jumbo=[string]$jumbo;
    filter_present=[bool]($bind -ne $null);
    filter_enabled=[bool]($bind -ne $null -and $bind.Enabled)
  }
}
$out | ConvertTo-Json -Compress
""".replace("%MATCH%", _FILTER_MATCH)

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
    # берём максимальное допустимое значение Jumbo Packet и ставим его
    ps = r"""
$ErrorActionPreference='Stop'
$p=Get-NetAdapterAdvancedProperty -Name '%NAME%' -DisplayName '*Jumbo*' | Select-Object -First 1
if(-not $p){ throw 'у адаптера нет настройки Jumbo Packet' }
$best=$p.ValidDisplayValues | Where-Object {$_ -match '\d'} | Sort-Object {[int]($_ -replace '\D','')} | Select-Object -Last 1
if(-not $best){ throw 'нет числовых значений Jumbo' }
Set-NetAdapterAdvancedProperty -Name '%NAME%' -DisplayName $p.DisplayName -DisplayValue $best
"$best"
""".replace("%NAME%", name.replace("'", "''"))
    code, out, err = _ps(ps)
    if code != 0:
        return {"ok": False, "error": err or "не удалось включить jumbo"}
    return {"ok": True, "jumbo": out.strip()}


def enable_filter(name):
    if not is_admin():
        return {"ok": False, "error": "нужны права администратора (запустите приложение от имени администратора)"}
    ps = r"""
$ErrorActionPreference='Stop'
$b=Get-NetAdapterBinding -Name '%NAME%' | Where-Object { $_.ComponentID -match '%MATCH%' -or $_.DisplayName -match '%MATCH%' } | Select-Object -First 1
if(-not $b){ throw 'фильтр-драйвер GigE (neugev) не найден среди привязок адаптера — установлен ли он?' }
Enable-NetAdapterBinding -Name '%NAME%' -ComponentID $b.ComponentID
$b.ComponentID
""".replace("%NAME%", name.replace("'", "''")).replace("%MATCH%", _FILTER_MATCH)
    code, out, err = _ps(ps)
    if code != 0:
        return {"ok": False, "error": err or "не удалось включить фильтр-драйвер"}
    return {"ok": True, "component": out.strip()}
