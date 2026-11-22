import json
import logging
import logging.handlers
import re
from collections import deque
from datetime import datetime
from threading import Lock


_events = deque(maxlen=1000)
_last_id = 0
_lock = Lock()


# пароль в адресе потока (rtsp://admin:пароль@10.0.0.5/...) в журнал и окно «Логи» не попадает
_URL_PASSWORD = re.compile(r"(://[^:/@\s]+:)[^@/\s]+@")


def mask_secrets(value):
    """Строки с rtsp://логин:пароль@хост → пароль заменён на ***; обходит словари и списки."""
    if isinstance(value, str):
        return _URL_PASSWORD.sub(r"\1***@", value)
    if isinstance(value, dict):
        return {k: mask_secrets(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_secrets(v) for v in value]
    return value


# ---- файловый журнал событий (logs\events.log): всё, что идёт через log_event, плюс срабатывания и переходы тревог мониторинга.
# Память держит лишь 1000 последних событий и пропадает при перезапуске — по файлу можно разбирать «задним числом», что и когда было.
_FILE_MAX_BYTES = 5 * 1024 * 1024
_FILE_BACKUPS = 5                    # events.log + events.log.1 … .5 (до ~30 МБ)
_PAYLOAD_MAX = 1500                  # длиннее — обрезаем, чтобы одна строка не раздула журнал
_file_logger = None
_file_last = None                    # (level, source, message) последней записанной строки — подряд идущие повторы склеиваем
_file_repeat = 0


def enable_file(path) -> bool:
    """Включить запись событий в файл с ротацией. Вызывается один раз при старте приложения. False — папку/файл открыть не удалось
    (приложение работает как раньше, журнал только в памяти)."""
    global _file_logger
    try:
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(str(path), maxBytes=_FILE_MAX_BYTES, backupCount=_FILE_BACKUPS, encoding="utf-8", errors="replace")
        h.setFormatter(logging.Formatter("%(message)s"))
        lg = logging.getLogger("web_mvs.events")
        lg.handlers.clear()
        lg.propagate = False
        lg.setLevel(logging.INFO)
        lg.addHandler(h)
        _file_logger = lg
        return True
    except Exception:
        _file_logger = None
        return False


def disable_file() -> None:
    global _file_logger, _file_last, _file_repeat
    lg, _file_logger = _file_logger, None
    _file_last, _file_repeat = None, 0
    if lg is not None:
        for h in list(lg.handlers):
            lg.removeHandler(h)
            h.close()


def _write_file(item: dict) -> None:
    """Одна строка: время [уровень] источник: сообщение {payload}. Ошибка записи журнал не роняет."""
    global _file_last, _file_repeat
    lg = _file_logger
    if lg is None:
        return
    try:
        key = (item["level"], item["source"], item["message"], json.dumps(item["payload"], ensure_ascii=False, default=str, sort_keys=True)[:_PAYLOAD_MAX])
        if key == _file_last:
            _file_repeat += 1
            return
        if _file_repeat:
            lg.info("%s    (предыдущая строка повторилась ещё %d раз)", item["time"].replace("T", " "), _file_repeat)
        _file_last, _file_repeat = key, 0
        line = "%s [%s] %s: %s" % (item["time"].replace("T", " "), item["level"], item["source"], item["message"])
        if item["payload"]:
            line += " " + key[3]
        lg.info(line.replace("\n", " "))
    except Exception:
        pass


def log_event(source: str, message: str, level: str = "info", payload: dict | None = None):
    global _last_id
    message = mask_secrets(message)
    payload = mask_secrets(payload) if payload else payload

    with _lock:
        _last_id += 1
        item = {
            "id": _last_id,
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "source": source,
            "level": level,
            "message": message,
            "payload": payload or {},
        }
        _events.append(item)
        _write_file(item)
        return item


def get_events(since_id: int = 0):
    with _lock:
        items = [item for item in _events if item["id"] > since_id]
        last_id = _last_id

    return {
        "items": items,
        "last_id": last_id,
    }