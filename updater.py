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
    zip_path = STAGING / str(asset.get("name", "web_MVS_new.zip"))

    try:
        log_event("updater.download", "Скачиваю релиз", "info", {"name": asset.get("name")})
        req = urllib.request.Request(asset["browser_download_url"],
                                     headers={"User-Agent": "web_MVS-update"})
        with urllib.request.urlopen(req, timeout=180) as resp, open(zip_path, "wb") as out:
            shutil.copyfileobj(resp, out)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(STAGED)
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
    run_bat = app_dir / "run.bat"
    relaunch = str(run_bat if run_bat.is_file() else (app_dir / "web_MVS.exe"))

    # PowerShell-скрипт: ждёт выхода приложения, заменяет файлы, перезапускает.
    # _internal может быть кратко занят при выходе — снимаем с повторами.
    ps = f"""$ErrorActionPreference = 'SilentlyContinue'
$app = '{app_dir}'
$staging = '{STAGING}'
$staged = '{STAGED}'
try {{ Wait-Process -Id {pid} -Timeout 60 }} catch {{}}
Start-Sleep -Seconds 1
for ($i = 0; $i -lt 20; $i++) {{
    try {{ Remove-Item -LiteralPath (Join-Path $app '_internal') -Recurse -Force -ErrorAction Stop; break }}
    catch {{ Start-Sleep -Milliseconds 500 }}
}}
Remove-Item -LiteralPath (Join-Path $app 'web_MVS.exe') -Force
Copy-Item -Path (Join-Path $staged '*') -Destination $app -Recurse -Force
Remove-Item -LiteralPath $staging -Recurse -Force
Start-Process -FilePath '{relaunch}' -WorkingDirectory $app
"""
    helper = Path(tempfile.gettempdir()) / f"web_mvs_apply_{pid}.ps1"
    # UTF-8 с BOM — иначе PowerShell 5.1 неверно прочитает не-ASCII в путях
    helper.write_bytes(b"\xef\xbb\xbf" + ps.encode("utf-8"))

    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(helper)],
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        close_fds=True,
    )
    log_event("updater.apply", "Запущен апдейтер; приложение завершается для замены файлов",
              "warn", {"helper": str(helper)})
    return {"ok": True}
