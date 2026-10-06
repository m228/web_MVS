"""cv_store — персист результатов CV-анализа проб + выборки для UI (галерея, тренд).

Раскладка (в DATA_DIR, переживает обновление, см. paths.py):
    <DATA_DIR>/cv_results/<serial>/<ts>/
        result.json        — сводка пробы + по-кадровые данные
        frame_0.jpg ...     — ЧИСТЫЕ кадры пробы (JPEG); контуры кристаллов рисует браузер по
                             objects_N.json (слои по группам, подсветка формы под мышкой)
        objects_0.json ...  — объекты кадра: размер, форма, группа, контур (poly)
        thumb.jpg          — миниатюра кадра 0 с контурами (лента проб в UI)
    Старые пробы: overlay_N.jpg / overlay_N.png — кадры с контурами, нарисованными в картинке.

Ротация: держим последние keep_last проб на серийник (старые удаляем).
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

import cv_volume
from logger import log_event
from paths import DATA_DIR

BASE = DATA_DIR / "cv_results"
HISTORY = DATA_DIR / "cv_history"      # лёгкий журнал проб по дням — НЕ чистится ротацией кадров
_HIST_LOCK = threading.Lock()
_HIST_FILLED = set()                   # серийники, для которых журнал уже дополнен из cv_results
TS_FMT = "%Y-%m-%d_%H_%M_%S"

GROUP_ORDER = ["small", "medium", "large", "reject"]
THUMB_W = 240      # ширина миниатюры пробы, px


def _serial_dir(serial: str) -> Path:
    tag = "".join(c for c in str(serial) if c.isalnum() or c in "-_.") or "camera"
    return BASE / tag


def _hist_dir(serial: str) -> Path:
    return HISTORY / _serial_dir(serial).name


def _ts_epoch(ts: str) -> Optional[float]:
    try:
        return time.mktime(time.strptime(ts, TS_FMT))
    except Exception:
        return None


def _hist_row(result: dict) -> Optional[dict]:
    """Одна строка журнала из result.json пробы: только числа для тренда (без кадров и объектов)."""
    t = _ts_epoch(result.get("ts", ""))
    if t is None:
        return None
    s = result.get("summary") or {}
    sz = s.get("size_um") or {}
    pct = s.get("groups_pct") or {}
    fr = (result.get("fracture") or {}).get("summary") or {}
    plc = result.get("plc") or {}
    rs = s.get("reasons") or {}
    vp = s.get("volume_pct") or {}
    return {
        "ts": result["ts"], "t": t, "stage": result.get("stage"), "sv": result.get("sv"),
        # причины брака (среднее число на кадр), разброс размера и плотность — для разбора слипания и обучения
        "n_needle": rs.get("needle"), "n_aggregate": rs.get("aggregate"), "n_crooked": rs.get("crooked"),
        "n_tiny": rs.get("tiny"), "n_huge": rs.get("huge"), "suspect": s.get("suspect"),
        "reject_pct": s.get("reject_pct"), "cv_pct": sz.get("cv_pct"), "density": s.get("density_per_mm2"),
        # режим варки на момент пробы (ПЛК): для разбора «почему слиплось» по серии проб
        "temp": plc.get("temp_app"), "level": plc.get("level"), "current": plc.get("current"),
        "vac": plc.get("press_top"), "cook_time": plc.get("cook_time"), "seed_age": plc.get("seed_age_s"),
        "count": s.get("count"), "mean": sz.get("mean"), "median": sz.get("median"),
        "small": pct.get("small"), "medium": pct.get("medium"),
        "large": pct.get("large"), "reject": pct.get("reject"),
        # мелочь и сростки по объёму (модели M1 шар / M2 сфероид / M3 призма) и по площади, % от общего
        "fines_m1": vp.get("m1", {}).get("fines"), "fines_m2": vp.get("m2", {}).get("fines"),
        "fines_m3": vp.get("m3", {}).get("fines"), "fines_area": vp.get("area", {}).get("fines"),
        "agg_m3": vp.get("m3", {}).get("agg"), "agg_area": vp.get("area", {}).get("agg"),
        "fines_n": vp.get("n", {}).get("fines"),
        "frac_zones": fr.get("zones"), "frac_pct": fr.get("area_pct"),
        "frames": len(result.get("frames") or []),
    }


def _hist_file(serial: str, ts: str) -> Path:
    return _hist_dir(serial) / (ts[:10] + ".jsonl")      # «2026-10-04»


def _hist_append(serial: str, result: dict):
    row = _hist_row(result)
    if row is None:
        return
    with _HIST_LOCK:
        f = _hist_file(serial, row["ts"])
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _hist_read(serial: str, day: str) -> list[dict]:
    f = _hist_dir(serial) / (day + ".jsonl")
    rows = []
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue            # битая строка (обрыв записи) не рушит весь день
    except Exception:
        pass
    return rows


def _hist_backfill(serial: str):
    """Один раз за запуск: добавить в журнал пробы, которые уже лежат в cv_results (до появления
    журнала). Идемпотентно: что уже есть в журнале — не дублируем."""
    if serial in _HIST_FILLED:
        return
    _HIST_FILLED.add(serial)
    sd = _serial_dir(serial)
    if not sd.exists():
        return
    known = set()
    if _hist_dir(serial).exists():
        for f in _hist_dir(serial).glob("*.jsonl"):
            known.update(r.get("ts") for r in _hist_read(serial, f.stem))
    for p in sorted(x for x in sd.iterdir() if x.is_dir()):
        if p.name in known:
            continue
        try:
            _hist_append(serial, _with_volume(p, json.loads((p / "result.json").read_text(encoding="utf-8"))))
        except Exception:
            continue


def history_days(serial: str) -> list[dict]:
    """Дни, за которые есть пробы: [{date, n, first, last}] по возрастанию даты (для выбора даты)."""
    _hist_backfill(serial)
    out = []
    if _hist_dir(serial).exists():
        for f in sorted(_hist_dir(serial).glob("*.jsonl")):
            rows = _hist_read(serial, f.stem)
            if rows:
                ts = [r["t"] for r in rows if r.get("t") is not None]
                out.append({"date": f.stem, "n": len(rows), "first": min(ts), "last": max(ts)})
    return out


def trend_range(serial: str, t_from: float, t_to: float, series: Optional[list[str]] = None,
                limit: int = 5000) -> dict:
    """Тренд по реальному времени: пробы с t_from по t_to (epoch, сек) из журнала по дням.
    Возвращает {t:[...], ts:[...], stage:[...], series:{name:[...]}}; не больше limit точек."""
    _hist_backfill(serial)
    series = series or ["small", "medium", "large", "reject", "mean", "median"]
    rows = []
    hd = _hist_dir(serial)
    # перебираем только существующие файлы дней (а не все даты в диапазоне): запрос «с 1970» не падает
    for f in (sorted(hd.glob("*.jsonl")) if hd.exists() else []):
        try:
            day0 = time.mktime(time.strptime(f.stem, "%Y-%m-%d"))
        except Exception:
            continue
        if day0 > t_to or day0 + 86400 < t_from:
            continue
        rows.extend(r for r in _hist_read(serial, f.stem)
                    if r.get("t") is not None and t_from <= r["t"] <= t_to)
    rows.sort(key=lambda r: r["t"])
    if len(rows) > limit:
        rows = rows[-limit:]
    return {
        "from": t_from, "to": t_to,
        "t": [r["t"] for r in rows], "ts": [r["ts"] for r in rows],
        "stage": [r.get("stage") for r in rows],
        "series": {s: [r.get(s) for r in rows] for s in series},
    }


EXPORT_COLUMNS = ["ts", "t", "stage", "sv", "temp", "level", "current", "vac", "cook_time", "seed_age",
                  "count", "mean", "median", "cv_pct", "density", "small", "medium", "large", "reject",
                  "reject_pct", "fines_m1", "fines_m2", "fines_m3", "fines_area", "agg_m3", "agg_area", "fines_n",
                  "n_needle", "n_aggregate", "n_crooked", "n_tiny", "n_huge", "suspect",
                  "frac_zones", "frac_pct", "frames"]


def export_rows(serial: str, t_from: Optional[float] = None, t_to: Optional[float] = None) -> list[dict]:
    """Все строки журнала проб (по времени) в заданном диапазоне — для выгрузки в CSV/обучение.
    Без границ — вся история. Колонки — EXPORT_COLUMNS; чего в старой пробе не было — None."""
    _hist_backfill(serial)
    rows = []
    hd = _hist_dir(serial)
    for f in (sorted(hd.glob("*.jsonl")) if hd.exists() else []):
        rows.extend(r for r in _hist_read(serial, f.stem) if r.get("t") is not None
                    and (t_from is None or r["t"] >= t_from) and (t_to is None or r["t"] <= t_to))
    rows.sort(key=lambda r: r["t"])
    return [{c: r.get(c) for c in EXPORT_COLUMNS} for r in rows]


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


REASONS = ["needle", "aggregate", "crooked", "tiny", "huge"]


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
    vol = cv_volume.empty_sums()
    for s in ok:
        vol = cv_volume.add_sums(vol, s.get("volume") or {})
    has_vol = any(s.get("volume") for s in ok)
    return {
        "count": round(sum(s["count"] for s in ok) / n, 1),
        "groups": counts,
        # объём: суммы по кадрам (а не среднее процентов — большие кристаллы весят больше)
        **({"volume": vol, "volume_pct": cv_volume.percents(vol),
            "volume_cfg": next((s["volume_cfg"] for s in ok if s.get("volume_cfg")), None)} if has_vol else {}),
        "groups_pct": {g: round(100.0 * counts[g] / total, 1) for g in GROUP_ORDER},
        "size_um": {
            "mean": round(mean_size, 1),
            "median": round(median_size, 1),
            "cv_pct": round(sum(s["size_um"].get("cv_pct", 0) for s in ok) / n, 1),
        },
        "density_per_mm2": round(sum(s["density_per_mm2"] for s in ok) / n, 2),
        # причины брака и вытянутые — средние по кадрам; reject_active — брак шёл в рассев хотя бы в одном кадре
        "reasons": {r: round(sum((s.get("reasons") or {}).get(r, 0) for s in ok) / n, 1) for r in REASONS},
        "suspect": round(sum(s.get("suspect", 0) for s in ok) / n, 1),
        "reject_active": any(s.get("reject_active", True) for s in ok),
        "reject_pct": round(sum(s["reject_pct"] for s in ok) / n, 1),
        "quality": "low" if any(s.get("quality") == "low" for s in ok) else "ok",
        "frames": n,
    }


def save_sample(serial: str, stage, frames: list[dict], images: list, timing: dict,
                keep_last: int = 50, ts: Optional[str] = None,
                fracture: Optional[dict] = None, jpeg_quality: int = 85,
                thumb_img=None, sv: Optional[float] = None,
                plc: Optional[dict] = None) -> Optional[dict]:
    """Сохранить пробу. frames — список {file, summary, objects}. images — ЧИСТЫЕ кадры пробы
    (numpy BGR), пишутся в JPEG (jpeg_quality); контуры поверх рисует браузер по objects_N.json.
    thumb_img — кадр с контурами для миниатюры (нет — миниатюра из чистого кадра 0).

    Возвращает {ts, dir, summary} или None при ошибке.
    """
    ts = ts or time.strftime("%Y-%m-%d_%H_%M_%S")
    d = _serial_dir(serial) / ts
    try:
        d.mkdir(parents=True, exist_ok=True)
        frame_recs = []
        for i, fr in enumerate(frames):
            ov_name = None
            if i < len(images) and images[i] is not None:
                ov_name = "frame_%d.jpg" % i
                # imencode + write_bytes, а не cv2.imwrite: тот ломается на путях с кириллицей
                ok, enc = cv2.imencode(".jpg", images[i],
                                       [cv2.IMWRITE_JPEG_QUALITY, int(jpeg_quality)])
                if not ok:
                    raise OSError("не удалось закодировать кадр в JPEG")
                (d / ov_name).write_bytes(enc.tobytes())
                if i == 0:
                    thumb = _encode_thumb(thumb_img if thumb_img is not None else images[i])
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
                "overlay": ov_name,          # файл кадра (имя поля — для совместимости с UI)
                "clean": True,               # кадр чистый: контуры рисует браузер по objects
                "summary": fr.get("summary"),
                "objects_n": len(objs),
            })
        summary = _aggregate(frames)
        result = {
            "serial": str(serial),
            "ts": ts,
            "stage": stage,
            "sv": round(sv, 1) if sv is not None else None,   # СВ пробы (от него зависит учёт брака)
            # режим варки из ПЛК на момент пробы: temp_app °C, level %, current A (ток циркулятора),
            # press_top (разрежение сверху), cook_time с, seed_age_s с (время с заводки). Нет ПЛК — ключа нет.
            **({"plc": plc} if plc else {}),
            "timing": timing,
            "summary": summary,
            "frames": frame_recs,
            "fracture": fracture or {"summary": {"zones": 0, "has_fracture": False, "area_pct": 0.0},
                                     "zones": []},
        }
        (d / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            _hist_append(serial, result)         # журнал трендов — переживает ротацию кадров
        except Exception as e:
            log_event("cv_store", "Не записана строка журнала трендов", "warn", {"error": str(e)})
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


def _volume_cfg_now() -> dict:
    """Настройки объёма из конфига (поля блока «Объём и мелочь»); нет конфига — дефолты."""
    try:
        import plate_config
        return {"volume": (plate_config.load().get("cv") or {}).get("volume") or {}}
    except Exception:
        return {}


def _with_volume(d: Path, r: dict, cfg: Optional[dict] = None) -> dict:
    """Проба, снятая до появления объёма (в summary нет volume): досчитать мелочь/сростки по
    сохранённым объектам кадров по ТЕКУЩИМ полям порога и k. Файлы пробы не меняем — только ответ."""
    s = (r or {}).get("summary")
    if not s or s.get("volume"):
        return r
    try:
        from types import SimpleNamespace as NS
        cfg = cfg if cfg is not None else _volume_cfg_now()
        tot = cv_volume.empty_sums()
        for i in range(len(r.get("frames") or [1])):
            objs = json.loads((d / ("objects_%d.json" % i)).read_text(encoding="utf-8"))
            ms = [NS(group=o.get("group"), defect=o.get("defect"), size_um=o["size_um"],
                     length_um=o["length_um"], width_um=o["width_um"]) for o in objs]
            tot = cv_volume.add_sums(tot, cv_volume.sums_for(ms, cfg))
        s["volume"], s["volume_pct"] = tot, cv_volume.percents(tot)
        s["volume_cfg"] = cv_volume.volume_cfg(cfg)
    except Exception:
        pass            # нет объектов/битый файл — проба просто без объёма
    return r


def list_samples(serial: str, limit: int = 50) -> list[dict]:
    """Список проб (новые сверху): [{ts, count, summary}]. Без тяжёлых по-кадровых данных."""
    sd = _serial_dir(serial)
    if not sd.exists():
        return []
    dirs = sorted([p for p in sd.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)
    out = []
    vcfg = _volume_cfg_now()
    for p in dirs[:limit]:
        try:
            r = _with_volume(p, json.loads((p / "result.json").read_text(encoding="utf-8")), vcfg)
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
        return _with_volume(d, json.loads((d / "result.json").read_text(encoding="utf-8")))
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
    # frame_N.jpg — текущий формат (чистый кадр); overlay_N.jpg/.png — старые пробы с контурами
    # в самой картинке
    for name in ("frame_%d.jpg", "overlay_%d.jpg", "overlay_%d.png"):
        p = d / (name % int(idx))
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
