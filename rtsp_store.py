"""Мини-база сохранённых RTSP-камер (простой JSON-файл рядом с программой).

Хранит строки подключения добавленных вручную RTSP-камер, чтобы после
перезапуска их не вводить заново. Ключ записи — URL подключения.
"""
import json
import os
import threading

from logger import log_event
from paths import DATA_DIR

# в каталоге пользовательских данных, чтобы база переживала обновление (см. paths.py)
STORE = DATA_DIR / "rtsp_cameras.json"

# сериализуем read-modify-write: фронт может дублировать save/remove параллельно,
# и без лока один запрос затрёт результат другого
_lock = threading.Lock()


def _read_strict():
    """Прочитать базу, ПРОБРАСЫВАЯ ошибку чтения/парсинга.

    Нужно для save/remove: там нельзя молча получить [] при сбое чтения —
    иначе read-modify-write затрёт всю базу одной записью (или очистит её).
    """
    if not STORE.is_file():
        return []
    data = json.loads(STORE.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def load():
    # мягкое чтение для отображения: при повреждении/сбое отдаём пустой список
    try:
        return _read_strict()
    except Exception as e:
        log_event("rtsp_store", "Не удалось прочитать базу RTSP", "warn", {"error": str(e)})
    return []


def _write(items):
    # атомарная запись: пишем во временный файл и подменяем — при сбое на середине
    # исходный JSON остаётся целым, а не превращается в обрезок (потеря всех камер)
    try:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE.parent / (STORE.name + ".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STORE)
    except Exception as e:
        log_event("rtsp_store", "Не удалось сохранить базу RTSP", "warn", {"error": str(e)})


def save(entry):
    url = (entry or {}).get("url")
    if not url:
        return load()
    with _lock:
        # строгое чтение: если база не читается — НЕ пишем, иначе затрём остальные камеры
        try:
            existing = _read_strict()
        except Exception as e:
            log_event("rtsp_store", "Чтение базы перед сохранением не удалось — запись отменена",
                      "error", {"url": url, "error": str(e)})
            return load()
        items = [i for i in existing if i.get("url") != url]
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
    with _lock:
        try:
            existing = _read_strict()
        except Exception as e:
            log_event("rtsp_store", "Чтение базы перед удалением не удалось — операция отменена",
                      "error", {"url": url, "error": str(e)})
            return load()
        items = [i for i in existing if i.get("url") != url]
        _write(items)
    log_event("rtsp_store", "RTSP-камера удалена из базы", "info", {"url": url})
    return items
