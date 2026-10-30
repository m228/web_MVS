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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

import delta_update
from logger import log_event
from paths import DATA_DIR, read_version

REPO = "m228/web_MVS"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_PREFIX = "web_MVS_"

STAGING = DATA_DIR / ".update"      # сюда качаем и распаковываем обновление
STAGED = STAGING / "staged"          # распакованное содержимое архива
# бэкап прежних _internal\ + exe (для отката). ВНЕ STAGING: apply в конце сносит
# STAGING целиком, а бэкап должен пережить сбой и остаться для восстановления.
BACKUP = DATA_DIR / ".update_backup"

# скачивание идёт в фоне, пока приложение работает (камера, пробы, цикл не прерываются); одновременно — только одно
_DL_LOCK = threading.Lock()


def staged_zip():
    """Скачанный и проверенный архив обновления (STAGING/web_MVS_*.zip) или None. Недокачанный лежит как *.part и не считается."""
    try:
        zs = sorted(STAGING.glob(ASSET_PREFIX + "*.zip")) if STAGING.is_dir() else []
    except OSError:
        return None
    return zs[-1] if zs else None


def staged_version() -> str:
    """Версия скачанного обновления (из _internal/VERSION внутри архива) или пустая строка."""
    z = staged_zip()
    return _zip_version(z) if z is not None else ""


def _zip_version(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.replace("\\", "/") == "_internal/VERSION":
                    return _norm(zf.read(name).decode("utf-8", "replace").strip())
    except Exception:
        pass
    return ""


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


def _version_tuple(version: str):
    """Числовые компоненты версии для сравнения ('1.10.0' > '1.9.9')."""
    nums = re.findall(r"\d+", _norm(version))
    return tuple(int(n) for n in nums) if nums else (0,)


_meta_cache = None  # (ts, (tag, asset, notes)) — чтобы download не дёргал API повторно


def _fetch_latest_meta(max_age: float = 0.0):
    """(tag, asset_dict|None, notes) последнего релиза с GitHub.

    max_age>0 позволяет переиспользовать недавний ответ (download после check),
    чтобы не делать второй запрос и гарантированно скачать ту же версию, что
    показана в UI — между двумя запросами релиз мог смениться.
    """
    global _meta_cache
    if max_age > 0 and _meta_cache is not None and (time.time() - _meta_cache[0]) < max_age:
        return _meta_cache[1]

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
    meta = (data.get("tag_name") or "", asset, data.get("body") or "")
    _meta_cache = (time.time(), meta)
    return meta


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
    # апдейт доступен только если релиз строго новее текущей — не предлагаем даунгрейд
    available = asset is not None and _version_tuple(latest) > _version_tuple(current)
    downloading = _DL_LOCK.locked()
    staged = "" if downloading else staged_version()
    if staged and _version_tuple(staged) <= _version_tuple(current) and STAGING.exists():
        shutil.rmtree(STAGING, ignore_errors=True)         # архив уже установлен — лишние ~80 МБ не держим
        staged = ""
    log_event("updater.check", "Проверка обновлений", "info",
              {"current": current, "latest": latest, "available": available})
    return {
        "current": current,
        "latest": latest or None,
        "update_available": available,
        "has_asset": asset is not None,
        "notes": notes,
        "staged": staged or None,              # версия, уже скачанная и готовая к перезапуску
        "downloading": downloading,            # скачивание идёт прямо сейчас (страницу могли перезагрузить)
    }


def _verify_download(zip_path: Path, asset: dict) -> None:
    """Сверить скачанный zip с метаданными релиза; бросить ValueError при расхождении.

    GitHub отдаёт для ассета поле digest вида 'sha256:<hex>' (не для всех старых
    релизов) — если оно есть, проверяем криптографически. Иначе — хотя бы сверяем
    размер (защита от обрыва скачивания) и пишем предупреждение в лог.
    Полная защита от подмены требует, чтобы релиз всегда содержал sha256 (build.bat).
    """
    expected_size = asset.get("size")
    actual_size = zip_path.stat().st_size
    if isinstance(expected_size, int) and expected_size > 0 and actual_size != expected_size:
        raise ValueError(f"размер архива {actual_size} != ожидаемого {expected_size}")

    digest = str(asset.get("digest") or "")
    if digest.startswith("sha256:"):
        expected = digest.split(":", 1)[1].strip().lower()
        h = hashlib.sha256()
        with open(zip_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        actual = h.hexdigest().lower()
        if actual != expected:
            raise ValueError(f"sha256 не совпал: {actual} != {expected}")
        log_event("updater.download", "Контрольная сумма подтверждена", "info", {"sha256": actual})
    else:
        log_event("updater.download", "У релиза нет sha256-дайджеста — проверка целостности пропущена",
                  "warn")


# верхняя граница размера, если GitHub не сообщил size (страховка от заполнения диска)
_MAX_DOWNLOAD_FALLBACK = 500 * 1024 * 1024   # 500 МБ
_DOWNLOAD_DEADLINE = 900                       # сек на весь трансфер (не на сокет)


def _download_stream(resp, out, max_bytes, deadline):
    """Читать ответ чанками с лимитом размера и общим дедлайном.

    urlopen(timeout=) — таймаут сокета (на одно чтение), он НЕ ограничивает общее
    время: при медленном, но непрерывном потоке скачивание висело бы бесконечно.
    Плюс без лимита размера подменённый/битый ответ мог бы забить диск.
    """
    total = 0
    while True:
        if time.time() > deadline:
            raise TimeoutError("превышено общее время скачивания")
        chunk = resp.read(1 << 20)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"размер загрузки превысил лимит {max_bytes} байт")
        out.write(chunk)
    return total


def download_latest(extract: bool = True, delta_only: bool = False):
    """Скачать последний релиз (и распаковать во временную папку — для apply_update; для кнопки «перезапустить» на странице
    микроскопа распаковка не нужна: update.ps1 сам распакует архив, extract=False). Только в бандле. Работает в фоне, приложение
    при этом не останавливается. Скачивание идёт в *.part и переименовывается только после проверки размера и sha256 —
    недокачанный файл не принимается за готовое обновление.
    delta_only — только докачка изменившихся файлов (несколько МБ вместо ~77): не вышла — сразу ответ delta_failed, без полной загрузки
    этим же способом (её тогда делает update.ps1, у него системный прокси и повторы)."""
    if not is_frozen():
        return {"ok": False, "error": "Обновление доступно только в собранной версии (.exe)"}
    if not _DL_LOCK.acquire(blocking=False):
        return {"ok": False, "busy": True, "error": "Скачивание уже идёт"}
    try:
        return _download_latest_locked(extract, delta_only)
    finally:
        _DL_LOCK.release()


def _try_delta(asset: dict, tag: str, size: int, part_path: Path, deadline: float):
    """Докачать только изменившиеся файлы (см. delta_update). None — не вышло или не выгодно: вызывающий качает архив целиком."""
    try:
        st = delta_update.fetch_delta(asset["browser_download_url"], size, _app_dir(), part_path, deadline)
        ver = _zip_version(part_path)
        if ver != _norm(tag):                            # собранный архив обязан быть именно той версии, что в релизе
            raise delta_update.DeltaError("версия в собранном архиве %r, ожидалась %r" % (ver, _norm(tag)))
    except Exception as exc:
        log_event("updater.download", "Дельта-обновление не вышло — качаю архив целиком", "info", {"reason": str(exc)})
        try:
            part_path.unlink()
        except OSError:
            pass
        return None
    log_event("updater.download", "Дельта-обновление: докачано %.1f МБ из %.1f МБ (%d из %d файлов)" % (
        st["fetched"] / 1e6, size / 1e6, st["changed"], st["files"]), "success", st)
    st["full_size"] = size
    return st


def _download_latest_locked(extract: bool, delta_only: bool = False):
    try:
        # переиспользуем свежий ответ от check_latest (последние 2 минуты),
        # чтобы не делать второй запрос и скачать ту же версию, что в UI
        tag, asset, _ = _fetch_latest_meta(max_age=120)
    except Exception as exc:
        return {"ok": False, "error": "Не удалось связаться с GitHub: " + str(exc)}
    if asset is None:
        return {"ok": False, "error": "В последнем релизе нет файла web_MVS_*.zip"}

    if STAGING.exists():
        shutil.rmtree(STAGING, ignore_errors=True)
    STAGING.mkdir(parents=True, exist_ok=True)
    # имя приходит из GitHub API — берём только basename, чтобы оно не увело файл из STAGING
    zip_path = STAGING / Path(str(asset.get("name") or "web_MVS_new.zip")).name
    part_path = zip_path.with_name(zip_path.name + ".part")

    try:
        log_event("updater.download", "Скачиваю релиз", "info", {"name": asset.get("name")})
        size = int(asset.get("size") or 0)
        deadline = time.time() + _DOWNLOAD_DEADLINE
        delta = _try_delta(asset, tag, size, part_path, deadline)
        if delta is None and delta_only:
            shutil.rmtree(STAGING, ignore_errors=True)
            return {"ok": False, "delta_failed": True, "error": "докачка изменившихся файлов не вышла"}
        if delta is None:
            req = urllib.request.Request(asset["browser_download_url"],
                                         headers={"User-Agent": "web_MVS-update"})
            # лимит: заявленный размер +5% на расхождения, иначе — страховочный потолок
            max_bytes = int(size * 1.05) if size > 0 else _MAX_DOWNLOAD_FALLBACK
            with urllib.request.urlopen(req, timeout=60) as resp, open(part_path, "wb") as out:
                _download_stream(resp, out, max_bytes, deadline)
            _verify_download(part_path, asset)
        os.replace(part_path, zip_path)                  # готово: только теперь архив считается скачанным
        if extract:
            with zipfile.ZipFile(zip_path) as zf:
                _safe_extract(zf, STAGED)
    except Exception as exc:
        log_event("updater.download", "Ошибка скачивания/распаковки", "error", {"error": str(exc)})
        try:
            part_path.unlink()
        except OSError:
            pass
        return {"ok": False, "error": "Ошибка скачивания: " + str(exc)}

    staged_ver = staged_version()
    if not staged_ver and extract:
        try:
            staged_ver = (STAGED / "_internal" / "VERSION").read_text(encoding="utf-8").strip()
        except Exception:
            pass
    log_event("updater.download", "Релиз скачан и проверен", "success", {"version": staged_ver, "extracted": extract, "delta": delta})
    res = {"ok": True, "version": staged_ver or _norm(tag)}
    if delta:
        res["delta"] = delta
    return res


def _ps_quote(value) -> str:
    """Экранировать значение для вставки внутрь одинарных кавычек PowerShell.

    В PS одинарная кавычка внутри '...' удваивается. Пути берутся из sys.executable,
    но имя пользователя может содержать апостроф (например O'Brien) — без экранирования
    такой путь разорвёт строку и сломает (в худшем случае — исказит) генерируемый скрипт.
    """
    return str(value).replace("'", "''")


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
$app = '{_ps_quote(app_dir)}'
$staging = '{_ps_quote(STAGING)}'
$staged = '{_ps_quote(STAGED)}'
$backup = '{_ps_quote(BACKUP)}'
$exe = '{_ps_quote(exe)}'
$log = '{_ps_quote(log_file)}'
function W($m) {{ "$(Get-Date -Format 'HH:mm:ss') $m" | Out-File -FilePath $log -Append -Encoding utf8 }}
W "=== apply: жду выхода приложения (pid {pid}) ==="
try {{ Wait-Process -Id {pid} -Timeout 60 }} catch {{}}
Start-Sleep -Seconds 1
$internal = Join-Path $app '_internal'
# бэкап держим рядом с данными (DATA_DIR\\.update\\backup), а НЕ в %TEMP%: он должен
# пережить сбой/убийство этого скрипта, чтобы приложение можно было восстановить.
# Снимаем прошлый (успешный) бэкап и создаём чистый.
Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $backup -Force | Out-Null

# бэкап текущих _internal и exe ПЕРЕМЕЩЕНИЕМ (быстро и обратимо) — чтобы при сбое
# копирования новой версии откатиться, а не остаться без рабочего приложения.
# Ждём освобождения файлов до $maxTries * $stepMs миллисекунд.
$maxTries = 30
$stepMs = 500
$ok = $true
for ($i = 0; $i -lt $maxTries; $i++) {{
    try {{
        if (Test-Path -LiteralPath $internal) {{
            Move-Item -LiteralPath $internal -Destination (Join-Path $backup '_internal') -Force -ErrorAction Stop
        }}
        if (Test-Path -LiteralPath $exe) {{
            Move-Item -LiteralPath $exe -Destination (Join-Path $backup 'web_MVS.exe') -Force -ErrorAction Stop
        }}
        break
    }} catch {{
        W "ожидание освобождения файлов: $_"
        Start-Sleep -Milliseconds $stepMs
        if ($i -eq ($maxTries - 1)) {{ $ok = $false; W "ОШИБКА: файлы не освободились за $($maxTries * $stepMs / 1000) c" }}
    }}
}}
if ($ok) {{
    try {{
        Copy-Item -Path (Join-Path $staged '*') -Destination $app -Recurse -Force -ErrorAction Stop
        W "файлы заменены"
    }} catch {{ W "ОШИБКА копирования: $_ — откатываюсь"; $ok = $false }}
}}
if (-not $ok) {{
    # откат: вернуть сохранённые _internal и exe на место
    if (Test-Path -LiteralPath (Join-Path $backup '_internal')) {{
        Remove-Item -LiteralPath $internal -Recurse -Force -ErrorAction SilentlyContinue
        Move-Item -LiteralPath (Join-Path $backup '_internal') -Destination $internal -Force -ErrorAction SilentlyContinue
    }}
    if (Test-Path -LiteralPath (Join-Path $backup 'web_MVS.exe')) {{
        Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue
        Move-Item -LiteralPath (Join-Path $backup 'web_MVS.exe') -Destination $exe -Force -ErrorAction SilentlyContinue
    }}
    W "выполнен откат к предыдущей версии"
}}
$restarted = $false
if (Test-Path -LiteralPath $exe) {{
    W "перезапуск $exe"
    try {{ Start-Process -FilePath $exe -WorkingDirectory $app -ErrorAction Stop; $restarted = $true }}
    catch {{ W "ОШИБКА перезапуска: $_" }}
}} else {{
    W "ОШИБКА: web_MVS.exe не найден после обновления"
}}
# бэкап удаляем ТОЛЬКО при успешном обновлении И успешном перезапуске; иначе
# оставляем его на диске для ручного восстановления (путь пишем в лог).
if ($ok -and $restarted) {{
    Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
}} else {{
    W "бэкап предыдущей версии сохранён: $backup"
}}
Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
W "=== apply: готово ==="
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
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
    # абсолютный путь к powershell.exe (а не через PATH) — на случай PATH hijacking:
    # apply запускается с админ-токеном, подмена powershell в PATH дала бы выполнение
    # чужого кода. Если по каким-то причинам файла нет — откат на поиск по PATH.
    ps_exe = os.path.expandvars(r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
    if not os.path.isfile(ps_exe):
        ps_exe = "powershell"
    cmd = [ps_exe, "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-WindowStyle", "Hidden", "-File", str(helper)]
    base = CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(cmd, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
    except OSError:
        subprocess.Popen(cmd, creationflags=base, close_fds=True)

    log_event("updater.apply", "Запущен апдейтер; приложение завершается для замены файлов",
              "warn", {"helper": str(helper)})
    return {"ok": True}


_LOG_LINE = re.compile(r"^\ufeff?(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)$")


def update_log_tail(since: float = 0.0, limit: int = 40) -> list:
    """Строки update.log (рядом с exe), записанные не раньше since (секунды Unix, местное время машины), без метки времени.
    Страница по ним показывает ход обновления (скачиваю… / ОШИБКА…): update.ps1 идёт минутами, а на скачивании и стопе приложения
    сама страница ничего не знает. Из исходников и без файла — пусто."""
    if not is_frozen():
        return []
    try:
        with open(_app_dir() / "update.log", "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            raw = f.read()
    except OSError:
        return []
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        m = _LOG_LINE.match(line.strip())
        if not m:
            continue
        try:
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            continue
        if ts >= since:
            out.append(m.group(2))
    return out[-max(1, int(limit)):]


# дополнения (сервисы рядом с приложением), которые update.ps1 умеет обновлять из того же релиза
ADDONS = [
    {"key": "cv", "title": "CV микроскопа (cv_service)", "dir": "cv_service", "marker": "detector.py", "heavy": True},
    {"key": "video", "title": "Видео-ролик микроскопа (video_service)", "dir": "video_service", "marker": "render.py", "heavy": False},
    {"key": "motion", "title": "CV RTSP / движение (motion_service)", "dir": "motion_service", "marker": "motion_engine.py", "heavy": False},
]
_COMPONENT_KEYS = {"main"} | {a["key"] for a in ADDONS}


def components_info() -> dict:
    """Что можно обновить: основное приложение + дополнения (стоят ли рядом, какой версии) и прошлый выбор из update_choice.json."""
    items = [{"key": "main", "title": "Основное приложение (web_MVS)", "installed": True, "version": read_version(), "heavy": False}]
    for a in ADDONS:
        d = DATA_DIR / a["dir"]
        ver = ""
        try:
            ver = (d / "VERSION").read_text(encoding="utf-8").strip().lstrip("vV")
        except OSError:
            pass
        items.append({"key": a["key"], "title": a["title"], "installed": (d / a["marker"]).is_file(), "version": ver,
                      "venv": (d / ".venv" / "Scripts" / "python.exe").is_file(), "heavy": a["heavy"]})
    remembered = []
    try:
        data = json.loads((DATA_DIR / "update_choice.json").read_text(encoding="utf-8-sig"))
        remembered = [k for k in (data.get("components") or []) if k in _COMPONENT_KEYS]
    except (OSError, ValueError, AttributeError):
        pass
    return {"components": items, "remembered": remembered}


def parse_components(value) -> list:
    """'main,cv' -> ['main', 'cv'] (только известные ключи, без повторов); пусто -> []."""
    out = []
    for k in re.split(r"[,;\s]+", str(value or "")):
        k = k.strip().lower()
        if k in _COMPONENT_KEYS and k not in out:
            out.append(k)
    return out


def run_update_script(port: int = 8000, staged: bool = False, components=None, yes: bool = False, no_browser: bool = False) -> dict:
    """Запустить update.ps1 из папки установки (тот же апдейтер, что update.bat): он сам остановит приложение, заменит файлы из
    последнего релиза, запустит приложение и дождётся ответа (при сбое откатит). Здесь только отвязанный запуск: сам скрипт
    приложение останавливает — выходить из процесса тут не нужно. Приложение работает с правами админа (манифест exe), поэтому
    скрипт наследует админ-токен и UAC-окно не показывает."""
    if not is_frozen():
        return {"ok": False, "error": "Обновление доступно только в собранной версии (.exe)"}
    app_dir = _app_dir()
    script = app_dir / "update.ps1"
    if not script.is_file():
        return {"ok": False, "error": "update.ps1 не найден рядом с программой (%s)" % app_dir}
    # абсолютный путь к powershell.exe (не через PATH): запуск идёт с админ-токеном
    ps_exe = os.path.expandvars(r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
    if not os.path.isfile(ps_exe):
        ps_exe = "powershell"
    comps = parse_components(components) or ["main"]            # без выбора — как раньше, только основное приложение
    if staged and "main" not in comps:
        return {"ok": False, "error": "Скачанный архив — это основное приложение; отметьте его или обновляйте дополнения отдельно"}
    cmd = [ps_exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script),
           "-Root", str(app_dir), "-Port", str(int(port)), "-NoMenu", "-Components", ",".join(comps)]
    if yes:                                      # тяжёлую первую установку CV пользователь подтвердил на странице
        cmd.append("-Yes")
    # no_browser: после обновления не открывать новую вкладку — страница, с которой нажали, сама дождётся и перезагрузится.
    # Первое обновление идёт СТАРЫМ update.ps1 (он заменяется последним) — незнакомый ключ уронил бы запуск, поэтому передаём его, только если скрипт его знает
    no_browser_applied = False
    if no_browser:
        try:
            no_browser_applied = "NoBrowser" in script.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            pass
        if no_browser_applied:
            cmd.append("-NoBrowser")
    if staged:                                   # быстрый перезапуск: архив уже скачан в фоне — скрипт не качает, только распаковывает и меняет
        zp = staged_zip()
        if zp is None:
            return {"ok": False, "error": "Сначала скачайте обновление"}
        cmd += ["-ZipPath", str(zp)]
    CREATE_NEW_CONSOLE, CREATE_NEW_PROCESS_GROUP, CREATE_BREAKAWAY_FROM_JOB = 0x00000010, 0x00000200, 0x01000000
    base = CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP         # своя (скрытая) консоль + своя группа: скрипт переживёт выход приложения
    try:
        subprocess.Popen(cmd, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, close_fds=True, cwd=str(app_dir))
    except OSError:                                              # job не разрешает breakaway (или job нет) — без него
        subprocess.Popen(cmd, creationflags=base, close_fds=True, cwd=str(app_dir))
    log_event("updater.run", "Запущен update.ps1 из папки установки" + ("; приложение будет остановлено и запущено заново" if "main" in comps else ""),
              "warn", {"script": str(script), "components": comps})
    return {"ok": True, "message": "обновление запущено", "components": comps, "restart": "main" in comps,
            "no_browser": no_browser_applied}
