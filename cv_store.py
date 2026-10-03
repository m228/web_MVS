"""cv_store — персист результатов CV-анализа проб + выборки для UI (галерея, тренд).

Раскладка (в DATA_DIR, переживает обновление, см. paths.py):
    <DATA_DIR>/cv_results/<serial>/<ts>/
        result.json        — сводка пробы + по-кадровые данные
        overlay_0.png ...   — кадры с обводкой кристаллов

Ротация: держим последние keep_last проб на серийник (старые удаляем).
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Optional

import cv2

from logger import log_event
from paths import DATA_DIR

BASE = DATA_DIR / "cv_results"

GROUP_ORDER = ["small", "medium", "large", "reject"]


def _serial_dir(serial: str) -> Path:
    tag = "".join(c for c in str(serial) if c.isalnum() or c in "-_.") or "camera"
    return BASE / tag


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
                fracture: Optional[dict] = None) -> Optional[dict]:
    """Сохранить пробу. frames — список {file, summary, objects}. overlays — numpy BGR по кадрам.

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
                ov_name = "overlay_%d.png" % i
                cv2.imwrite(str(d / ov_name), overlays[i])
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
    try:
        return json.loads((_serial_dir(serial) / ts / "result.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def get_last(serial: str) -> Optional[dict]:
    lst = list_samples(serial, limit=1)
    return get_result(serial, lst[0]["ts"]) if lst else None


def get_prev(serial: str) -> Optional[dict]:
    lst = list_samples(serial, limit=2)
    return get_result(serial, lst[1]["ts"]) if len(lst) > 1 else None


def overlay_path(serial: str, ts: str, idx: int = 0) -> Optional[Path]:
    p = _serial_dir(serial) / ts / ("overlay_%d.png" % idx)
    return p if p.exists() else None


def get_objects(serial: str, ts: str, idx: int = 0) -> list:
    """Объекты (кристаллы) кадра пробы — для наведения в UI (bbox/size_um/area_um2)."""
    try:
        return json.loads((_serial_dir(serial) / ts / ("objects_%d.json" % idx)).read_text(encoding="utf-8"))
    except Exception:
        return []


def trend(serial: str, series: Optional[list[str]] = None, limit: int = 200) -> dict:
    """Серии для интерактивного тренда: точки во времени по выбранным метрикам.

    series — какие линии вернуть: подмножество групп + 'mean' (средний размер).
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
            elif s == "frac_zones":
                data[s].append(frs.get("zones"))
            elif s == "frac_pct":
                data[s].append(frs.get("area_pct"))
            else:
                data[s].append((summ.get("groups_pct") or {}).get(s))
    return {"ts": ts_list, "stage": stage_list, "series": data}
