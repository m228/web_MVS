"""cv_store — персист результатов CV-анализа проб + выборки для UI (галерея, тренд).

Раскладка (в DATA_DIR, переживает обновление, см. paths.py):
    <DATA_DIR>/cv_results/<serial>/<ts>/
        result.json        — сводка пробы + по-кадровые данные
        overlay_0.jpg ...   — кадры с обводкой кристаллов (JPEG; старые пробы — .png)
        objects_0.json ...  — объекты кадра (для наведения в UI)
        thumb.jpg          — миниатюра кадра 0 (лента проб в UI)

Ротация: держим последние keep_last проб на серийник (старые удаляем).
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from logger import log_event
from paths import DATA_DIR

BASE = DATA_DIR / "cv_results"

GROUP_ORDER = ["small", "medium", "large", "reject"]
THUMB_W = 240      # ширина миниатюры пробы, px


def _serial_dir(serial: str) -> Path:
    tag = "".join(c for c in str(serial) if c.isalnum() or c in "-_.") or "camera"
    return BASE / tag


def _probe_dir(serial: str, ts: str) -> Optional[Path]:
    """Папка пробы по метке времени. ts приходит из запроса — пускаем только «цифры/_/-»,
    чтобы через него нельзя было выйти из каталога проб."""
    if not re.fullmatch(r"[0-9_-]+", str(ts or "")):
        return None
    return _serial_dir(serial) / ts


def _encode_thumb(img) -> Optional[bytes]:
    h, w = img.shape[:2]
    if w > THUMB_W:
        img = cv2.resize(img, (THUMB_W, max(1, int(h * THUMB_W / w))), interpolation=cv2.INTER_AREA)
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return enc.tobytes() if ok else None


def _aggregate(frames: list[dict]) -> dict:
    """Сводка пробы = усреднение по-кадровых сводок (кадры одной пробы ~ одинаковы)."""
    ok = [f["summary"] for f in frames if f.get("summary")]
    if not ok:
        return {"count": 0, "groups": {g: 0 for g in GROUP_ORDER}}
    n = len(ok)
    counts = {g: round(sum(s["groups"].get(g, 0) for s in ok) / n, 1) for g in GROUP_ORDER}
    total = sum(counts.values()) or 1
    mean_size = sum(s["size_um"]["mean"] for s in ok) / n
    median_size = sum(s["size_um"]["median"] for s in ok) / n
    return {
        "count": round(sum(s["count"] for s in ok) / n, 1),
        "groups": counts,
        "groups_pct": {g: round(100.0 * counts[g] / total, 1) for g in GROUP_ORDER},
        "size_um": {
            "mean": round(mean_size, 1),
            "median": round(median_size, 1),
            "cv_pct": round(sum(s["size_um"].get("cv_pct", 0) for s in ok) / n, 1),
        },
        "density_per_mm2": round(sum(s["density_per_mm2"] for s in ok) / n, 2),
        "reject_pct": round(sum(s["reject_pct"] for s in ok) / n, 1),
        "quality": "low" if any(s.get("quality") == "low" for s in ok) else "ok",
        "frames": n,
    }


def save_sample(serial: str, stage, frames: list[dict], overlays: list, timing: dict,
                keep_last: int = 50, ts: Optional[str] = None,
                fracture: Optional[dict] = None, jpeg_quality: int = 85) -> Optional[dict]:
    """Сохранить пробу. frames — список {file, summary, objects}. overlays — numpy BGR по кадрам.

    Overlay пишется в JPEG (jpeg_quality) — в разы легче PNG; сырой кадр не хранится.
    Возвращает {ts, dir, summary} или None при ошибке.
    """
    ts = ts or time.strftime("%Y-%m-%d_%H_%M_%S")
    d = _serial_dir(serial) / ts
    try:
        d.mkdir(parents=True, exist_ok=True)
        frame_recs = []
        for i, fr in enumerate(frames):
            ov_name = None
            if i < len(overlays) and overlays[i] is not None:
                ov_name = "overlay_%d.jpg" % i
                # imencode + write_bytes, а не cv2.imwrite: тот ломается на путях с кириллицей
                ok, enc = cv2.imencode(".jpg", overlays[i],
                                       [cv2.IMWRITE_JPEG_QUALITY, int(jpeg_quality)])
                if not ok:
                    raise OSError("не удалось закодировать overlay в JPEG")
                (d / ov_name).write_bytes(enc.tobytes())
                if i == 0:
                    thumb = _encode_thumb(overlays[i])
                    if thumb:
                        (d / "thumb.jpg").write_bytes(thumb)
            # объекты кадра — в отдельный файл (для наведения в UI), чтобы result.json был лёгким
            objs = fr.get("objects") or []
            if objs:
                (d / ("objects_%d.json" % i)).write_text(
                    json.dumps(objs, ensure_ascii=False), encoding="utf-8")
            frame_recs.append({
                "idx": i,
                "src": fr.get("file"),
                "overlay": ov_name,
                "summary": fr.get("summary"),
                "objects_n": len(objs),
            })
        summary = _aggregate(frames)
        result = {
            "serial": str(serial),
            "ts": ts,
            "stage": stage,
            "timing": timing,
            "summary": summary,
            "frames": frame_recs,
            "fracture": fracture or {"summary": {"zones": 0, "has_fracture": False, "area_pct": 0.0},
                                     "zones": []},
        }
        (d / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        _rotate(serial, keep_last)
        log_event("cv_store", "Проба CV сохранена", "info",
                  {"serial": str(serial), "ts": ts, "count": summary.get("count")})
        return {"ts": ts, "dir": str(d), "summary": summary}
    except Exception as e:
        log_event("cv_store", "Ошибка сохранения пробы CV", "error", {"error": str(e)})
        return None


def _rotate(serial: str, keep_last: int):
    sd = _serial_dir(serial)
    if not sd.exists():
        return
    dirs = sorted([p for p in sd.iterdir() if p.is_dir()], key=lambda p: p.name)
    for old in dirs[:-keep_last] if keep_last > 0 else []:
        shutil.rmtree(old, ignore_errors=True)


def list_samples(serial: str, limit: int = 50) -> list[dict]:
    """Список проб (новые сверху): [{ts, count, summary}]. Без тяжёлых по-кадровых данных."""
    sd = _serial_dir(serial)
    if not sd.exists():
        return []
    dirs = sorted([p for p in sd.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)
    out = []
    for p in dirs[:limit]:
        try:
            r = json.loads((p / "result.json").read_text(encoding="utf-8"))
            out.append({"ts": r["ts"], "stage": r.get("stage"),
                        "summary": r.get("summary"), "frames": len(r.get("frames", [])),
                        "fracture": (r.get("fracture") or {}).get("summary")})
        except Exception:
            continue
    return out


def get_result(serial: str, ts: str) -> Optional[dict]:
    d = _probe_dir(serial, ts)
    if d is None:
        return None
    try:
        return json.loads((d / "result.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def get_last(serial: str) -> Optional[dict]:
    lst = list_samples(serial, limit=1)
    return get_result(serial, lst[0]["ts"]) if lst else None


def get_prev(serial: str) -> Optional[dict]:
    lst = list_samples(serial, limit=2)
    return get_result(serial, lst[1]["ts"]) if len(lst) > 1 else None


def overlay_path(serial: str, ts: str, idx: int = 0) -> Optional[Path]:
    d = _probe_dir(serial, ts)
    if d is None:
        return None
    # .jpg — текущий формат; .png — пробы, сохранённые до перехода на JPEG
    for ext in ("jpg", "png"):
        p = d / ("overlay_%d.%s" % (int(idx), ext))
        if p.exists():
            return p
    return None


def thumb_path(serial: str, ts: str) -> Optional[Path]:
    """Миниатюра пробы (кадр 0) для ленты проб. У проб, сохранённых до появления ленты, её
    нет — делаем из overlay при первом запросе и кладём рядом."""
    d = _probe_dir(serial, ts)
    if d is None:
        return None
    p = d / "thumb.jpg"
    if p.exists():
        return p
    src = overlay_path(serial, ts, 0)
    if not src:
        return None
    try:
        img = cv2.imdecode(np.frombuffer(src.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
        data = _encode_thumb(img) if img is not None else None
        if not data:
            return None
        p.write_bytes(data)
        return p
    except Exception:
        return None


def get_objects(serial: str, ts: str, idx: int = 0) -> list:
    """Объекты (кристаллы) кадра пробы — для наведения в UI (bbox/size_um/area_um2)."""
    d = _probe_dir(serial, ts)
    if d is None:
        return []
    try:
        return json.loads((d / ("objects_%d.json" % int(idx))).read_text(encoding="utf-8"))
    except Exception:
        return []


def trend(serial: str, series: Optional[list[str]] = None, limit: int = 200) -> dict:
    """Серии для интерактивного тренда: точки во времени по выбранным метрикам.

    series — какие линии вернуть: подмножество групп + 'mean' (средний размер) + 'count'
    (число кристаллов).
    Возвращает {ts:[...], stage:[...], series:{name:[values]}}.
    """
    series = series or ["small", "medium", "large", "reject", "mean"]
    samples = list_samples(serial, limit=limit)
    samples.reverse()   # по возрастанию времени для графика
    ts_list, stage_list = [], []
    data = {s: [] for s in series}
    for smp in samples:
        summ = smp.get("summary") or {}
        ts_list.append(smp["ts"])
        stage_list.append(smp.get("stage"))
        frs = smp.get("fracture") or {}
        for s in series:
            if s == "mean":
                data[s].append((summ.get("size_um") or {}).get("mean"))
            elif s == "count":
                data[s].append(summ.get("count"))
            elif s == "frac_zones":
                data[s].append(frs.get("zones"))
            elif s == "frac_pct":
                data[s].append(frs.get("area_pct"))
            else:
                data[s].append((summ.get("groups_pct") or {}).get(s))
    return {"ts": ts_list, "stage": stage_list, "series": data}
