"""Приёмник удалённого мониторинга: заводские web_MVS шлют сюда «пульс» (sysmon.py), панель показывает, что с каждым ПК.

Запускается на ПК Макса (не на заводе), своим venv: setup.bat один раз, run.bat — запуск. Слушает 127.0.0.1:8769 (MONITOR_HOST / MONITOR_PORT);
наружу его выводит туннель Cloudflare или обратный прокси с HTTPS (см. README.md).
Данные — data\\monitor.db (MONITOR_DATA), пароль панели — data\\server.json (или переменная MONITOR_PASSWORD). Нет пароля — создаётся и печатается при запуске.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import secrets
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

log = logging.getLogger("monitor")


def read_version() -> str:
    for p in (HERE.parent / "VERSION", HERE / "VERSION"):      # рядом с полным репозиторием web_MVS или с копией папки
        try:
            return p.read_text(encoding="utf-8").strip() or "dev"
        except OSError:
            continue
    return "dev"


def load_listen_hosts(data: Path) -> tuple[list[str], int]:
    """Где слушать: MONITOR_HOST / MONITOR_PORT главнее; иначе host (один адрес) или hosts (список) и port из data\\server.json
    (удобно для автозапуска, где окружения нет); иначе 127.0.0.1:8769. Для ZeroTier в host пишется адрес этого ПК в сети ZeroTier
    (например 172.29.134.237): тогда приёмник виден только там и на самом ПК через этот адрес. Список hosts — когда клиенты
    ещё сидят в двух сетях ZeroTier (переезд): слушаем адрес в каждой."""
    hosts, port = ["127.0.0.1"], 8769
    try:
        cfg = json.loads((data / "server.json").read_text(encoding="utf-8"))
        listed = cfg.get("hosts")
        if isinstance(listed, list):
            hosts = [str(h).strip() for h in listed if str(h).strip()] or hosts
        elif cfg.get("host"):
            hosts = [str(cfg["host"]).strip()]
        port = int(cfg.get("port") or port)
    except (OSError, ValueError, AttributeError):
        pass
    env = [h.strip() for h in os.environ.get("MONITOR_HOST", "").split(",") if h.strip()]
    return env or hosts, int(os.environ.get("MONITOR_PORT") or port)


def load_listen(data: Path) -> tuple[str, int]:
    """Первый адрес для прослушивания и порт (по нему трей проверяет, что приёмник жив)."""
    hosts, port = load_listen_hosts(data)
    return hosts[0], port


def load_enroll_key(data: Path) -> str:
    """Ключ регистрации клиентов: MONITOR_ENROLL_KEY главнее; иначе data\\server.json (enroll_key); иначе создаём и сохраняем.
    Тот же ключ build.bat кладёт в сборку клиента: без него заявка не принимается."""
    env = os.environ.get("MONITOR_ENROLL_KEY", "").strip()
    if env:
        return env
    path = data / "server.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    key = str(cfg.get("enroll_key") or "").strip()
    if not key:
        key = secrets.token_urlsafe(24)
        cfg["enroll_key"] = key
        data.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return key


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


def pid_alive(pid: int) -> bool:
    """Жив ли процесс с таким номером (для «осиротевшего» сервера: надзор в трее пропал — сервер не должен висеть на порту без управления)."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x00100000 | 0x1000, False, pid)         # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(ctypes.c_void_p(h), 0) == 0x102     # WAIT_TIMEOUT: ещё работает
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def watch_parent(pid: int, interval_s: float = 5.0, on_gone=None) -> threading.Thread:
    """Если процесс-надзор (tray.py) исчез, сервер выходит сам."""
    gone = on_gone or (lambda: os._exit(0))

    def loop():
        while True:
            time.sleep(interval_s)
            if not pid_alive(pid):
                log.warning("надзор (pid %d) пропал — завершаюсь", pid)
                gone()
                return

    t = threading.Thread(target=loop, name="parent-watch", daemon=True)
    t.start()
    return t


def build_app():
    import monitor_app
    import monitor_release
    import monitor_store
    import monitor_telegram
    data =Path(os.environ.get("MONITOR_DATA") or HERE / "data")
    tg = monitor_telegram.load_config(data)                    # блок "telegram" в data\server.json; нет — приёмник работает без уведомлений
    store = monitor_store.Store(data / "monitor.db", notify={"fresh_s": tg["fresh_s"], "offline_repeat_s": tg["offline_repeat_s"]} if tg else None)
    releases = monitor_release.Releases()                      # последняя версия на GitHub: метка «старая версия» в панели и в боте
    notifier = monitor_telegram.Notifier(store, monitor_telegram.TelegramClient(tg["token"], tg["api_base"]), tg, releases=releases) if tg else None
    bot = None
    if tg:                                                    # тот же бот отвечает на кнопки заводов (длинный опрос, наружу порты не открываем)
        bot = monitor_telegram.Bot(store, monitor_telegram.TelegramClient(tg["token"], tg["api_base"]),
                                   monitor_telegram.TelegramClient(tg["token"], tg["api_base"], timeout=monitor_telegram.Bot.POLL_S + 10), tg["chat_ids"],
                                   releases=releases)
    log.info("Telegram: %s", "подключён, чатов: %d" % len(tg["chat_ids"]) if tg else "не настроен (telegram_setup.bat)")
    if tg:
        hm = tg.get("morning")
        log.info("Утренняя сводка: %s", "%02d:%02d" % hm if hm else "выключена")
    html = (HERE / "panel.html").read_text(encoding="utf-8")
    return monitor_app.create_app(store, load_password(data), read_version(), html, assets=load_assets(), notifier=notifier, bot=bot,
                                  releases=releases, enroll_key=load_enroll_key(data))


BIND_WAIT_S = 10            # адрес ZeroTier при загрузке Windows поднимается позже приёмника: пробуем ещё
BIND_TRIES = 6


def bind_all(uvicorn, app, hosts: list[str], port: int, sleep=time.sleep) -> list:
    """Сокеты на каждый адрес. Адрес, которого ещё нет (сеть ZeroTier не поднялась), ждём до BIND_TRIES·BIND_WAIT_S секунд, потом
    работаем на тех, что есть; ни одного нет — ошибка (надзор перезапустит). Так выход из одной из двух сетей не валит приёмник."""
    socks: dict = {}
    for attempt in range(BIND_TRIES):
        for h in hosts:
            if h in socks:
                continue
            try:
                # log_level обязателен: Config без него при создании сбрасывает логгеры uvicorn на INFO, и в service.log пошёл бы журнал доступа
                socks[h] = uvicorn.Config(app, host=h, port=port, log_level="warning").bind_socket()
            except OSError as e:
                log.warning("не удалось занять %s:%d (%s)%s", h, port, e, "" if attempt == BIND_TRIES - 1 or len(hosts) == 1 else ", повторю")
        if len(socks) == len(hosts) or len(hosts) == 1:
            break
        if attempt < BIND_TRIES - 1:
            sleep(BIND_WAIT_S)
    if not socks:
        raise OSError("ни один адрес не занят: %s" % ", ".join(hosts))
    return list(socks.values())


def main():
    import uvicorn
    if sys.stdout is None or sys.stderr is None:         # запуск без окна (pythonw из автозапуска): консоли нет, uvicorn на ней падает
        sys.stdout = sys.stderr = open(HERE / "service.log", "a", encoding="utf-8", buffering=1)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    hosts, port = load_listen_hosts(Path(os.environ.get("MONITOR_DATA") or HERE / "data"))
    log.info("Приёмник мониторинга v%s: %s", read_version(), ", ".join("http://%s:%d" % (h, port) for h in hosts))
    if os.environ.get("MONITOR_PARENT_PID", "").isdigit():
        watch_parent(int(os.environ["MONITOR_PARENT_PID"]))
    app = build_app()
    if len(hosts) == 1:
        uvicorn.run(app, host=hosts[0], port=port, log_level="warning")
    else:
        uvicorn.Server(uvicorn.Config(app, host=hosts[0], port=port, log_level="warning")).run(sockets=bind_all(uvicorn, app, hosts, port))


if __name__ == "__main__":
    main()
