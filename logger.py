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
        return item


def get_events(since_id: int = 0):
    with _lock:
        items = [item for item in _events if item["id"] > since_id]
        last_id = _last_id

    return {
        "items": items,
        "last_id": last_id,
    }