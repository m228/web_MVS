"""fracture_lab — калибровка детектора разломов по СВОИМ кадрам (без боевой пробы).

Идея: оператор ломает кристаллы стеклом в Ручном режиме и снимает кадры («как РАЗЛОМ»), а целые — «как НОРМА».
Кадры хранятся чистыми PNG в <DATA_DIR>/fracture_lab/ рядом с метками. Для выбранных порогов детект считается
на сервере (cv_fracture.detect_zones), а вес тёмности/текстуры и порог уверенности пересчитываются прямо в
браузере по D и T каждой зоны — ползунки двигаются мгновенно. Подбор порогов — тоже по этим D/T.
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

import cv2
import numpy as np

import cv_fracture
from logger import log_event
from paths import DATA_DIR

LAB_DIR = DATA_DIR / "fracture_lab"
LABELS = ("fracture", "ok", "unknown")
_NAME = re.compile(r"fr_\d{8}_\d{6}(?:_\d+)?\.png")
_img_cache: dict = {}        # name -> BGR (небольшой кэш: детект на кадре 5 Мп не дешёвый)
_zone_cache: dict = {}       # (name, dark_thr, min_area_frac) -> зоны


def _check(name: str) -> str:
    if not _NAME.fullmatch(str(name or "")):
        raise ValueError("недопустимое имя кадра")
    return name


def _meta_path(name: str):
    return LAB_DIR / (name[:-4] + ".json")


def _read_meta(name: str) -> dict:
    try:
        m = json.loads(_meta_path(name).read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _write_meta(name: str, meta: dict):
    _meta_path(name).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _forget(name: str):
    _img_cache.pop(name, None)
    for k in [k for k in _zone_cache if k[0] == name]:
        _zone_cache.pop(k, None)


def save_frame(img: np.ndarray, label: str = "unknown", meta: Optional[dict] = None) -> str:
    """Сохранить кадр (чистый PNG) с меткой. Возвращает имя файла. meta — доп. поля метаданных (например, разметка зон)."""
    label = label if label in LABELS else "unknown"
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    base = time.strftime("fr_%Y%m%d_%H%M%S")
    name, n = base + ".png", 1
    while (LAB_DIR / name).exists():
        name, n = "%s_%d.png" % (base, n), n + 1
    ok, enc = cv2.imencode(".png", img)       # imencode + write_bytes: не ломается на путях с кириллицей
    if not ok:
        raise OSError("не удалось закодировать кадр")
    (LAB_DIR / name).write_bytes(enc.tobytes())
    _write_meta(name, {**(meta or {}), "label": label, "ts": time.time()})
    log_event("fracture_lab", "Кадр калибровки разломов сохранён", "info", {"name": name, "label": label})
    return name


def save_probe_sample(key: str, img: np.ndarray, label: str, extra: dict) -> str:
    """Кадр пробы, размеченный оператором вручную (удалил/принял/нарисовал зоны), — в калибровку: подбор порогов по
    разметке (вкладка «Разломы») берёт его как обычный размеченный кадр. Одна проба — один кадр: повторная правка
    обновляет метку и разметку, а не плодит файлы. extra: зоны оператора, удалённые автозоны."""
    meta = {**extra, "probe": key, "source": "operator_edit"}
    if LAB_DIR.exists():
        for p in LAB_DIR.glob("fr_*.json"):
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(m, dict) and m.get("probe") == key and (LAB_DIR / (p.stem + ".png")).exists():
                name = p.stem + ".png"
                _write_meta(name, {**meta, "label": label if label in LABELS else "unknown", "ts": time.time()})
                return name
    return save_frame(img, label, meta)


def list_frames() -> list[dict]:
    """Кадры калибровки, новые сверху: [{name, label, ts}]."""
    if not LAB_DIR.exists():
        return []
    out = []
    for p in sorted(LAB_DIR.glob("fr_*.png"), reverse=True):
        m = _read_meta(p.name)
        out.append({"name": p.name, "label": m.get("label", "unknown") if m.get("label") in LABELS else "unknown",
                    "ts": m.get("ts") or p.stat().st_mtime})
    return out


def set_label(name: str, label: str) -> dict:
    _check(name)
    if label not in LABELS:
        raise ValueError("метка: fracture / ok / unknown")
    if not (LAB_DIR / name).exists():
        raise FileNotFoundError(name)
    m = _read_meta(name)
    m["label"] = label
    _write_meta(name, m)
    return {"name": name, "label": label}


def delete(name: str):
    _check(name)
    for p in (LAB_DIR / name, _meta_path(name)):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    _forget(name)


def _image(name: str) -> np.ndarray:
    _check(name)
    img = _img_cache.get(name)
    if img is None:
        img = cv2.imdecode(np.frombuffer((LAB_DIR / name).read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("кадр не читается")
        if len(_img_cache) >= 4:
            _img_cache.pop(next(iter(_img_cache)))
        _img_cache[name] = img
    return img


def jpeg(name: str, quality: int = 82) -> bytes:
    """Кадр для показа в браузере (JPEG, исходный размер — координаты зон совпадают с пикселями)."""
    ok, enc = cv2.imencode(".jpg", _image(name), [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise OSError("не удалось закодировать JPEG")
    return enc.tobytes()


def zones(name: str, dark_thr: float, min_area_frac: float, with_poly: bool = True) -> list[dict]:
    """Зоны-кандидаты на кадре при данных dark_thr / min_area_frac (веса и порог уверенности клиент
    применяет сам по D и T). Кэшируется по (кадр, dark_thr, min_area)."""
    key = (_check(name), round(float(dark_thr), 3), round(float(min_area_frac), 6))
    zs = _zone_cache.get(key)
    if zs is None:
        zs = cv_fracture.detect_zones(_image(name), {"dark_thr": key[1], "min_area_frac": key[2]})
        _zone_cache[key] = zs
    if with_poly:
        return zs
    return [{k: v for k, v in z.items() if k not in ("poly", "bbox")} for z in zs]


def all_zones(dark_thr: float, min_area_frac: float) -> list[dict]:
    """Зоны по всем РАЗМЕЧЕННЫМ кадрам (fracture / ok) — для оценки и подбора порогов."""
    out = []
    for f in list_frames():
        if f["label"] in ("fracture", "ok"):
            out.append({"name": f["name"], "label": f["label"],
                        "zones": zones(f["name"], dark_thr, min_area_frac, with_poly=False)})
    return out
