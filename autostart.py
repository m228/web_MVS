"""Автозапуск приложения вместе с Windows — через Планировщик задач.

Почему планировщик, а НЕ реестр (`HKCU\\...\\Run`) и не папка «Автозагрузка»:
exe собран с манифестом requireAdministrator (uac_admin=True в web_MVS.spec).
Автозапуск такого exe обычными способами при каждом входе даёт UAC-запрос (или
молча не стартует вовсе). Задача планировщика с «наивысшими правами» (/RL HIGHEST)
поднимает elevated-процесс без запроса.

Триггер — ONLOGON (вход пользователя), а не ONSTART: это десктопное приложение,
ему нужна пользовательская сессия.

Работает только в собранной поставке (frozen): из исходников автозапускать нечего,
sys.executable — это python.exe. По образцу net_tools.py: subprocess со списком
аргументов, без окна консоли.
"""
import re
import subprocess
import sys

from logger import log_event

# имя задачи в планировщике (оно же путь /TN)
TASK_NAME = "web_MVS"
# флаг для run.py: не открывать браузер при автозапуске (иначе окно на каждый вход)
NO_BROWSER_FLAG = "--no-browser"

# чтобы не мигало окно консоли при запуске schtasks из GUI-процесса
_CREATE_NO_WINDOW = 0x08000000


def _decode(raw):
    """Вывод schtasks в кодировке консоли (на русской Windows — cp866), не UTF-8."""
    if not raw:
        return ""
    for encoding in ("utf-8", "cp866", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def _run(args, timeout=20):
    try:
        result = subprocess.run(
            args, capture_output=True, timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
        return result.returncode, _decode(result.stdout).strip(), _decode(result.stderr).strip()
    except FileNotFoundError:
        return 1, "", "schtasks не найден (не Windows?)"
    except subprocess.TimeoutExpired:
        return 1, "", f"schtasks превысил таймаут ({timeout} c)"
    except Exception as exc:
        return 1, "", str(exc)


def _is_frozen():
    return bool(getattr(sys, "frozen", False))


def app_command():
    """Команда автозапуска: путь к exe + флаг «без браузера»."""
    return f'"{sys.executable}" {NO_BROWSER_FLAG}'


def _task_command():
    """Команда, прописанная в существующей задаче (или None).

    Читаем XML (`/XML`), а не текстовый вывод: текстовый локализован, а в XML
    команда всегда лежит в теге <Command> независимо от языка Windows.
    """
    code, out, _ = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"])
    if code != 0 or not out:
        return None
    match = re.search(r"<Command>(.*?)</Command>", out, re.DOTALL)
    if not match:
        return None
    command = match.group(1).strip()
    args = re.search(r"<Arguments>(.*?)</Arguments>", out, re.DOTALL)
    return f'"{command}" {args.group(1).strip()}' if args else f'"{command}"'


def status():
    """Есть ли задача автозапуска и указывает ли она на текущий exe."""
    if not _is_frozen():
        return {"available": False, "enabled": False, "reason": "not_frozen",
                "hint": "автозапуск доступен только в собранной версии программы"}

    code, out, err = _run(["schtasks", "/Query", "/TN", TASK_NAME])
    enabled = code == 0
    data = {"available": True, "enabled": enabled, "task": TASK_NAME,
            "command": app_command()}

    if enabled:
        current = _task_command()
        data["task_command"] = current
        # папку приложения могли перенести — задача осталась со старым путём.
        # Лечится повторным включением галочки, поэтому просто предупреждаем.
        if current and sys.executable.lower() not in current.lower():
            data["stale_path"] = True
            data["hint"] = "задача указывает на другой путь — включите галочку заново"
    elif err:
        data["error"] = None if "cannot find" in err.lower() or "не найден" in err.lower() else err

    return data


def enable():
    """Создать (или пересоздать) задачу автозапуска при входе в Windows."""
    if not _is_frozen():
        log_event("autostart.enable", "Автозапуск недоступен: программа запущена из исходников", "warn")
        return {"ok": False, "available": False, "reason": "not_frozen",
                "error": "автозапуск доступен только в собранной версии программы"}

    code, out, err = _run([
        "schtasks", "/Create",
        "/TN", TASK_NAME,
        "/TR", app_command(),
        "/SC", "ONLOGON",
        "/RL", "HIGHEST",
        "/F",
    ])
    if code != 0:
        log_event("autostart.enable", "Не удалось включить автозапуск", "error",
                  {"error": err or out, "command": app_command()})
        return {"ok": False, "error": err or out or "не удалось создать задачу"}

    log_event("autostart.enable", "Автозапуск при входе в Windows включён", "success",
              {"task": TASK_NAME, "command": app_command()})
    return {"ok": True, "enabled": True, "task": TASK_NAME, "command": app_command()}


def disable():
    """Удалить задачу автозапуска."""
    if not _is_frozen():
        return {"ok": False, "available": False, "reason": "not_frozen"}

    code, out, err = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    if code != 0:
        log_event("autostart.disable", "Не удалось выключить автозапуск", "error",
                  {"error": err or out})
        return {"ok": False, "error": err or out or "не удалось удалить задачу"}

    log_event("autostart.disable", "Автозапуск при входе в Windows выключен", "success",
              {"task": TASK_NAME})
    return {"ok": True, "enabled": False}
