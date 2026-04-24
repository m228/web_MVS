from collections import deque
from datetime import datetime
from threading import Lock


_events = deque(maxlen=1000)
_last_id = 0
_lock = Lock()


def log_event(source: str, message: str, level: str = "info", payload: dict | None = None):
    global _last_id

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