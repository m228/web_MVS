"""Мини-база сохранённых RTSP-камер (простой JSON-файл рядом с программой).

Хранит строки подключения добавленных вручную RTSP-камер, чтобы после
перезапуска их не вводить заново. Ключ записи — URL подключения.
"""
import json
from pathlib import Path

from logger import log_event

STORE = Path(__file__).resolve().parent / "rtsp_cameras.json"


def load():
    try:
        if STORE.is_file():
            data = json.loads(STORE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception as e:
        log_event("rtsp_store", "Не удалось прочитать базу RTSP", "warn", {"error": str(e)})
    return []


def _write(items):
    try:
        STORE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log_event("rtsp_store", "Не удалось сохранить базу RTSP", "warn", {"error": str(e)})


def save(entry):
    url = (entry or {}).get("url")
    if not url:
        return load()
    items = [i for i in load() if i.get("url") != url]
    items.append({
        "url": url,
        "label": entry.get("label") or "",
        "ip": entry.get("ip") or "",
        "scale": entry.get("scale") or 100,
        "fps": entry.get("fps") or 0,
    })
    _write(items)
    log_event("rtsp_store", "RTSP-камера сохранена в базу", "info", {"url": url})
    return items


def remove(url):
    if not url:
        return load()
    items = [i for i in load() if i.get("url") != url]
    _write(items)
    log_event("rtsp_store", "RTSP-камера удалена из базы", "info", {"url": url})
    return items
