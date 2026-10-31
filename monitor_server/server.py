"""Приёмник удалённого мониторинга: заводские web_MVS шлют сюда «пульс» (sysmon.py), панель показывает, что с каждым ПК.

Запускается на ПК Макса (не на заводе), своим venv: setup.bat один раз, run.bat — запуск. Слушает 127.0.0.1:8769 (MONITOR_HOST / MONITOR_PORT);
наружу его выводит туннель Cloudflare или обратный прокси с HTTPS (см. README.md).
Данные — data\\monitor.db (MONITOR_DATA), пароль панели — data\\server.json (или переменная MONITOR_PASSWORD). Нет пароля — создаётся и печатается при запуске.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import monitor_app  # noqa: E402
import monitor_store  # noqa: E402

log = logging.getLogger("monitor")


def read_version() -> str:
    for p in (HERE.parent / "VERSION", HERE / "VERSION"):      # рядом с полным репозиторием web_MVS или с копией папки
        try:
            return p.read_text(encoding="utf-8").strip() or "dev"
        except OSError:
            continue
    return "dev"


def load_password(data: Path) -> str:
    """Пароль панели: MONITOR_PASSWORD главнее; иначе data\\server.json; иначе создаём случайный и сохраняем."""
    env = os.environ.get("MONITOR_PASSWORD", "").strip()
    if env:
        return env
    path = data / "server.json"
    try:
        pw = str(json.loads(path.read_text(encoding="utf-8")).get("password") or "").strip()
        if pw:
            return pw
    except (OSError, ValueError, AttributeError):
        pass
    pw = secrets.token_urlsafe(12)
    data.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"password": pw}, ensure_ascii=False, indent=2), encoding="utf-8")
    # в окне run.bat пароль показываем, чтобы его можно было сразу взять; в файл журнала (службу, перенаправленный вывод) он не попадает
    shown = pw if sys.stdout is not None and sys.stdout.isatty() else "(смотри файл)"
    print("Пароль панели создан и сохранён в %s: %s" % (path, shown), flush=True)
    return pw


def load_assets() -> dict:
    """Иконки панели из static\\ (рисует tools\\make_icons.py): путь на сайте -> (байты, тип)."""
    files = {"/icon-192.png": ("icon-192.png", "image/png"), "/icon-512.png": ("icon-512.png", "image/png"),
             "/favicon.ico": ("panel.ico", "image/x-icon")}
    out = {}
    for url, (name, ctype) in files.items():
        try:
            out[url] = ((HERE / "static" / name).read_bytes(), ctype)
        except OSError:
            log.warning("нет файла static\\%s — иконка не будет показана", name)
    return out


def build_app():
    data = Path(os.environ.get("MONITOR_DATA") or HERE / "data")
    store = monitor_store.Store(data / "monitor.db")
    html = (HERE / "panel.html").read_text(encoding="utf-8")
    return monitor_app.create_app(store, load_password(data), read_version(), html, assets=load_assets())


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host = os.environ.get("MONITOR_HOST", "127.0.0.1")
    port = int(os.environ.get("MONITOR_PORT", "8769"))
    log.info("Приёмник мониторинга v%s: http://%s:%d", read_version(), host, port)
    uvicorn.run(build_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
