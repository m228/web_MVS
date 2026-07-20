"""Настройки автосохранения фото/видео (простой JSON-файл рядом с программой).

Хранит по каждой камере (ключ — серийник) последнее имя проекта и
интервал/длительность, чтобы после перезапуска не вводить их заново.
По образцу rtsp_store.py: атомарная запись, строгое чтение перед изменением.
"""
import json
import os
import threading

from logger import log_event
from paths import DATA_DIR

# в каталоге пользовательских данных, чтобы настройки пережили обновление (см. paths.py)
STORE = DATA_DIR / "save_settings.json"

# поля, которые храним на камеру
_FIELDS = ("photo_project", "photo_interval", "video_project", "video_duration")

# сериализуем read-modify-write: фронт может дублировать запросы параллельно,
# и без лока один запрос затрёт результат другого
_lock = threading.Lock()


def _read_strict():
    """Прочитать стор, ПРОБРАСЫВАЯ ошибку чтения/парсинга.

    Нужно для update: там нельзя молча получить {} при сбое чтения —
    иначе read-modify-write затрёт все настройки одной записью.
    """
    if not STORE.is_file():
        return {}
    data = json.loads(STORE.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def load():
    # мягкое чтение для отображения: при повреждении/сбое отдаём пустой словарь
    try:
        return _read_strict()
    except Exception as e:
        log_event("save_settings", "Не удалось прочитать настройки сохранения", "warn", {"error": str(e)})
    return {}


def get(serial_number):
    """Настройки для одной камеры (или пустой словарь)."""
    if not serial_number:
        return {}
    entry = load().get(serial_number)
    return entry if isinstance(entry, dict) else {}


def _write(data):
    # атомарная запись: пишем во временный файл и подменяем — при сбое на середине
    # исходный JSON остаётся целым, а не превращается в обрезок
    try:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE.parent / (STORE.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STORE)
    except Exception as e:
        log_event("save_settings", "Не удалось сохранить настройки сохранения", "warn", {"error": str(e)})


def update(serial_number, **fields):
    """Слить переданные поля в запись камеры и сохранить. Возвращает запись камеры."""
    if not serial_number:
        return {}
    # оставляем только известные поля с непустым значением
    patch = {k: v for k, v in fields.items() if k in _FIELDS and v not in (None, "")}
    if not patch:
        return get(serial_number)

    with _lock:
        # строгое чтение: если стор не читается — НЕ пишем, иначе затрём остальные камеры
        try:
            data = _read_strict()
        except Exception as e:
            log_event("save_settings", "Чтение настроек перед записью не удалось — запись отменена",
                      "error", {"serial_number": serial_number, "error": str(e)})
            return {}
        entry = data.get(serial_number)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry.update(patch)
        data[serial_number] = entry
        _write(data)
    return entry
