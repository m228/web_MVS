"""Самообновление собранного приложения из релизов GitHub.

Поток из трёх шагов:
  1. check    — узнать последнюю версию в релизах и сравнить с текущей;
  2. download — скачать zip последнего релиза и распаковать во временную папку;
  3. apply    — запустить ОТДЕЛЁННЫЙ (detached) скрипт, который дождётся выхода
               приложения, заменит файлы и перезапустит его.

Шаг apply работает только в собранном бандле (frozen): запущенный exe не может
перезаписать сам себя, поэтому замену делает внешний PowerShell-скрипт уже после
выхода приложения. Из исходников доступен только check.

Зависимостей нет — только стандартная библиотека (urllib, zipfile, shutil).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from logger import log_event
from paths import DATA_DIR, read_version

REPO = "m228/web_MVS"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_PREFIX = "web_MVS_"

STAGING = DATA_DIR / ".update"      # сюда качаем и распаковываем обновление
STAGED = STAGING / "staged"          # распакованное содержимое архива


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Распаковать zip, отклоняя записи с путём за пределы dest (zip-slip).

    Python 3.11 не поддерживает extractall(filter='data') (появился в 3.12),
    поэтому проверяем цель каждой записи вручную. Архив приходит из сети —
    а распаковывается в каталог приложения, работающего от админа.
    """
    dest = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if target != dest and dest not in target.parents:
            raise ValueError(f"Небезопасный путь в архиве: {member.filename!r}")
    zf.extractall(dest)


def _app_dir() -> Path:
    """Папка, где лежат web_MVS.exe и _internal\\ (рядом с exe)."""
    return Path(sys.executable).resolve().parent


def _norm(version: str) -> str:
    return (version or "").strip().lstrip("vV")


def _fetch_latest_meta():
    """(tag, asset_dict|None, notes) последнего релиза с GitHub."""
    req = urllib.request.Request(API_LATEST, headers={
        "User-Agent": "web_MVS-update",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    asset = None
    for item in data.get("assets", []):
        name = str(item.get("name", ""))
        if name.startswith(ASSET_PREFIX) and name.endswith(".zip"):
            asset = item
            break
    return data.get("tag_name") or "", asset, data.get("body") or ""


def check_latest():
    """Сравнить текущую версию с последним релизом. Безопасно из исходников."""
    current = read_version()
    try:
        tag, asset, notes = _fetch_latest_meta()
    except Exception as exc:
        log_event("updater.check", "Не удалось проверить обновления", "warn", {"error": str(exc)})
        return {"current": current, "latest": None, "update_available": False,
                "error": "Не удалось связаться с GitHub: " + str(exc)}

    latest = _norm(tag)
    available = bool(latest) and asset is not None and _norm(current) != latest
    log_event("updater.check", "Проверка обновлений", "info",
              {"current": current, "latest": latest, "available": available})
    return {
        "current": current,
        "latest": latest or None,
        "update_available": available,
        "has_asset": asset is not None,
        "notes": notes,
    }


def download_latest():
    """Скачать и распаковать последний релиз во временную папку (только в бандле)."""
    if not is_frozen():
        return {"ok": False, "error": "Обновление доступно только в собранной версии (.exe)"}
    try:
        tag, asset, _ = _fetch_latest_meta()
    except Exception as exc:
        return {"ok": False, "error": "Не удалось связаться с GitHub: " + str(exc)}
    if asset is None:
        return {"ok": False, "error": "В последнем релизе нет файла web_MVS_*.zip"}

    if STAGING.exists():
        shutil.rmtree(STAGING, ignore_errors=True)
    STAGING.mkdir(parents=True, exist_ok=True)
    # имя приходит из GitHub API — берём только basename, чтобы оно не увело файл из STAGING
    zip_path = STAGING / Path(str(asset.get("name") or "web_MVS_new.zip")).name

    try:
        log_event("updater.download", "Скачиваю релиз", "info", {"name": asset.get("name")})
        req = urllib.request.Request(asset["browser_download_url"],
                                     headers={"User-Agent": "web_MVS-update"})
        with urllib.request.urlopen(req, timeout=180) as resp, open(zip_path, "wb") as out:
            shutil.copyfileobj(resp, out)
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extract(zf, STAGED)
    except Exception as exc:
        log_event("updater.download", "Ошибка скачивания/распаковки", "error", {"error": str(exc)})
        return {"ok": False, "error": "Ошибка скачивания: " + str(exc)}

    staged_ver = ""
    try:
        staged_ver = (STAGED / "_internal" / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        pass
    log_event("updater.download", "Релиз скачан и распакован", "success", {"version": staged_ver})
    return {"ok": True, "version": staged_ver or _norm(tag)}


def apply_update():
    """Запустить отделённый апдейтер. Вызвавший эндпоинт после этого должен завершить
    процесс, чтобы апдейтер смог заменить файлы и перезапустить приложение."""
    if not is_frozen():
        return {"ok": False, "error": "Обновление доступно только в собранной версии (.exe)"}
    if not (STAGED / "web_MVS.exe").is_file():
        return {"ok": False, "error": "Сначала скачайте обновление"}

    app_dir = _app_dir()
    pid = os.getpid()
    exe = app_dir / "web_MVS.exe"
    log_file = app_dir / "update.log"

    # PowerShell-апдейтер: ждёт выхода приложения, заменяет файлы и перезапускает exe
    # НАПРЯМУЮ. Раньше перезапуск шёл через run.bat с запросом UAC в фоне — в detached
    # без окна повышение прав молча проваливалось, и приложение не стартовало. Теперь
    # права даёт встроенный в exe манифест (uac_admin), а detached-хелпер и так наследует
    # админ-токен запущенного приложения. Ход пишем в update.log рядом с exe — для отладки.
    ps = f"""$ErrorActionPreference = 'Continue'
$app = '{app_dir}'
$staging = '{STAGING}'
$staged = '{STAGED}'
$exe = '{exe}'
$log = '{log_file}'
function W($m) {{ "$(Get-Date -Format 'HH:mm:ss') $m" | Out-File -FilePath $log -Append -Encoding utf8 }}
W "=== apply: жду выхода приложения (pid {pid}) ==="
try {{ Wait-Process -Id {pid} -Timeout 60 }} catch {{}}
Start-Sleep -Seconds 1
for ($i = 0; $i -lt 30; $i++) {{
    try {{
        if (Test-Path -LiteralPath (Join-Path $app '_internal')) {{
            Remove-Item -LiteralPath (Join-Path $app '_internal') -Recurse -Force -ErrorAction Stop
        }}
        break
    }} catch {{ Start-Sleep -Milliseconds 500 }}
}}
Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue
try {{
    Copy-Item -Path (Join-Path $staged '*') -Destination $app -Recurse -Force -ErrorAction Stop
    W "файлы заменены"
}} catch {{ W "ОШИБКА копирования: $_" }}
Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $exe) {{
    W "перезапуск $exe"
    Start-Process -FilePath $exe -WorkingDirectory $app
}} else {{
    W "ОШИБКА: web_MVS.exe не найден после обновления"
}}
W "=== apply: готово ==="
"""
    helper = Path(tempfile.gettempdir()) / f"web_mvs_apply_{pid}.ps1"
    # UTF-8 с BOM — иначе PowerShell 5.1 неверно прочитает не-ASCII в путях
    helper.write_bytes(b"\xef\xbb\xbf" + ps.encode("utf-8"))

    CREATE_NEW_CONSOLE = 0x00000010
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    # ВАЖНО: хелперу нужна СВОЯ консоль (CREATE_NEW_CONSOLE) — без консоли PowerShell-хост
    # молча не инициализируется и скрипт не выполняется (проверено: с DETACHED_PROCESS
    # хелпер не стартовал, update.log не появлялся). Окно прячем через -WindowStyle Hidden.
    # Своя консоль + новая группа также отвязывают хелпер от приложения, чтобы он пережил
    # его выход. CREATE_BREAKAWAY_FROM_JOB — на случай kill-on-close job; если job его не
    # разрешает (или job нет), CreateProcess падает с OSError → откатываемся без него.
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-WindowStyle", "Hidden", "-File", str(helper)]
    base = CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(cmd, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
    except OSError:
        subprocess.Popen(cmd, creationflags=base, close_fds=True)

    log_event("updater.apply", "Запущен апдейтер; приложение завершается для замены файлов",
              "warn", {"helper": str(helper)})
    return {"ok": True}
