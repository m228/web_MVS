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
import db
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


def _volume_cols(s: dict, vp: dict, n_frames: int) -> dict:
    """Столбцы журнала по объёму: доли мелочи/сростков (% от общего) по трём моделям, площади и числу;
    общий объём на кадр (мм³) и с какими fines_side_mm/k_thick считали — чтобы потом пересчитать и сверить."""
    sums, cfg = s.get("volume") or {}, s.get("volume_cfg") or {}
    out = {}
    for kind in ("fines", "agg"):
        for key in ("m1", "m2", "m3", "area"):
            out["%s_%s" % (kind, key)] = (vp.get(key) or {}).get(kind)
        out["%s_n" % kind] = (vp.get("n") or {}).get(kind)
    for key in ("m1", "m2", "m3"):
        tot = (sums.get(key) or {}).get("total")
        out["vtot_" + key] = round(tot / 1e9 / n_frames, 4) if tot else None
    sv = (vp.get("sieve") or {})
    for mod in ("m1", "m2", "m3", "area"):           # рассев по ситам, %: sieve_<m1|m2|m3|area>_b0 (дно) … b6 (>1,2 мм); area — доля по площади
        for i in range(len(cv_volume.SIEVE_MM) + 1):
            out["sieve_%s_b%d" % (mod, i)] = (sv.get(mod) or [None] * (len(cv_volume.SIEVE_MM) + 1))[i]
    out["good_n"], out["rej_n"] = (sums.get("n") or {}).get("total"), (sums.get("n") or {}).get("agg")     # хороших кристаллов / отсеянного брака в пробе (штук, все кадры)
    out["fines_side_mm"], out["fines_um"] = cfg.get("fines_side_mm"), cfg.get("fines_um")
    out["k_thick"], out["fines_from_sv"] = cfg.get("k_thick"), cfg.get("fines_from_sv")
    return out


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
    # в журнал — мука и рассев БЕЗ порога СВ (он применяется при показе по варке, _apply_fines_gate)
    vp = (cv_volume.percents(s["volume"], ignore_off=True) if s.get("volume") else s.get("volume_pct")) or {}
    return {
        "ts": result["ts"], "t": t, "stage": result.get("stage"), "sv": result.get("sv"),
        # причины брака (среднее число на кадр), разброс размера и плотность — для разбора слипания и обучения
        "n_needle": rs.get("needle"), "n_aggregate": rs.get("aggregate"), "n_crooked": rs.get("crooked"),
        "n_tiny": rs.get("tiny"), "n_huge": rs.get("huge"), "suspect": s.get("suspect"),
        "reject_pct": s.get("reject_pct"), "cv_pct": sz.get("cv_pct"), "density": s.get("density_per_mm2"),
        # режим варки на момент пробы (ПЛК): для разбора «почему слиплось» по серии проб
        "temp": plc.get("temp_app"), "level": plc.get("level"), "current": plc.get("current"),
        "vac": plc.get("press_top"), "cook_time": plc.get("cook_time"), "substage": plc.get("substage"), "seed_age": plc.get("seed_age_s"),
        "count": s.get("count"), "mean": sz.get("mean"), "median": sz.get("median"),
        "small": pct.get("small"), "medium": pct.get("medium"),
        "large": pct.get("large"), "reject": pct.get("reject"),
        # мелочь и сростки по объёму (модели M1 шар / M2 сфероид / M3 призма) и по площади, % от общего
        **_volume_cols(s, vp, len(result.get("frames") or []) or 1),
        "frac_zones": fr.get("zones"), "frac_pct": fr.get("area_pct"),
        "frames": len(result.get("frames") or []),
    }


def _hist_file(serial: str, ts: str) -> Path:
    return _hist_dir(serial) / (ts[:10] + ".jsonl")      # «2026-10-04»


# Хранилище журнала проб: cv.storage = "files" (только jsonl, как раньше) | "both" (читаем файлы, пишем и в SQLite — для сверки
# «до и после») | "sqlite" (читаем и пишем только базу; кадры проб по-прежнему файлами). По умолчанию "both".
_STORAGE_CACHE = [0.0, "both"]


def _storage() -> str:
    now = time.time()
    if now - _STORAGE_CACHE[0] > 3:
        try:
            import plate_config
            v = str((plate_config.load().get("cv") or {}).get("storage", "both"))
        except Exception:
            v = "both"
        _STORAGE_CACHE[0], _STORAGE_CACHE[1] = now, (v if v in ("files", "both", "sqlite") else "both")
    return _STORAGE_CACHE[1]


def _use_files() -> bool:
    return _storage() in ("files", "both")


def _use_db() -> bool:
    return _storage() in ("both", "sqlite")


def _read_from_db() -> bool:
    return _storage() == "sqlite"


def _hist_append(serial: str, result: dict):
    row = _hist_row(result)
    if row is None:
        return
    if _use_files():
        with _HIST_LOCK:
            f = _hist_file(serial, row["ts"])
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    if _use_db():
        try:
            db.upsert_row(_serial_dir(serial).name, row)
        except Exception as e:
            log_event("cv_store", "Не записана строка журнала в SQLite", "warn", {"error": str(e)})


def compare_storage(serial: str) -> dict:
    """Сверка «до и после»: журнал в файлах (jsonl) и в SQLite — число проб, чего где нет, чем строки отличаются по ключевым числам."""
    tag = _serial_dir(serial).name
    hd = _hist_dir(serial)
    files = {}
    for f in (sorted(hd.glob("*.jsonl")) if hd.exists() else []):
        for r in _read_jsonl(f):
            if r.get("ts") is not None and r.get("t") is not None:
                files[r["ts"]] = r
    dbr = {r["ts"]: r for r in db.read_rows(tag)}
    keys = ("sv", "stage", "substage", "count", "mean", "fines_m1", "fines_m2", "fines_m3", "reject_pct", "frac_zones", "frac_pct", "cook_time")
    diff = [ts for ts in files.keys() & dbr.keys() if any(files[ts].get(k) != dbr[ts].get(k) for k in keys)]
    return {"serial": tag, "files": len(files), "sqlite": len(dbr), "only_files": len(files.keys() - dbr.keys()),
            "only_sqlite": len(dbr.keys() - files.keys()), "different": len(diff), "sample_different": sorted(diff)[:5],
            "ok": not (files.keys() ^ dbr.keys()) and not diff, "storage": _storage()}


def _hist_days(serial: str) -> list[str]:
    """Даты (YYYY-MM-DD), за которые есть пробы в журнале, по возрастанию."""
    if _read_from_db():
        return db.days(_serial_dir(serial).name)
    hd = _hist_dir(serial)
    return [f.stem for f in sorted(hd.glob("*.jsonl"))] if hd.exists() else []


_null_sv_outliers = db.null_sv_outliers      # правило сбойного СВ — в db.py (там же считается boil_id)


def _hist_read(serial: str, day: str, clean: bool = True) -> list[dict]:
    if _read_from_db():
        rows = db.read_rows(_serial_dir(serial).name, day=day)
        return _null_sv_outliers(rows) if clean else rows
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
    return _null_sv_outliers(rows) if clean else rows       # clean=False — сырые строки (для перезаписи журнала)


def sync_files_to_db(serial: str) -> int:
    """Залить в SQLite строки журнала из jsonl, которых там ещё нет (миграция и сверка «до и после»). Идемпотентно:
    ключ (serial, ts). Возвращает число добавленных строк. Файлы не меняет."""
    tag = _serial_dir(serial).name
    have, rows = db.known_ts(tag), []
    hd = _hist_dir(serial)
    for f in (sorted(hd.glob("*.jsonl")) if hd.exists() else []):
        for r in _read_jsonl(f):
            if r.get("ts") not in have and r.get("t") is not None:
                have.add(r["ts"])
                rows.append(r)
    return db.upsert_many(tag, rows) if rows else 0


def _read_jsonl(f: Path) -> list[dict]:
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
    журнала). Идемпотентно: что уже есть в журнале — не дублируем. Заодно журнал из файлов заливается в SQLite."""
    if serial in _HIST_FILLED:
        return
    _HIST_FILLED.add(serial)
    if _use_db():
        try:
            sync_files_to_db(serial)
        except Exception as e:
            log_event("cv_store", "Не удалось залить журнал в SQLite", "warn", {"error": str(e)})
    sd = _serial_dir(serial)
    if not sd.exists():
        return
    try:
        recompute_journal(serial)       # логика объёма/муки/рассева изменилась (calc_ver) — обновить строки, у которых ещё есть кадры
    except Exception:
        pass
    known = set()
    for day in _hist_days(serial):
        known.update(r.get("ts") for r in _hist_read(serial, day))
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
    for day in _hist_days(serial):
        rows = _hist_read(serial, day)
        if rows:
            ts = [r["t"] for r in rows if r.get("t") is not None]
            out.append({"date": day, "n": len(rows), "first": min(ts), "last": max(ts)})
    return out


_FINES_KEYS = ("fines_m1", "fines_m2", "fines_m3", "fines_area", "fines_n",
               "sieve_m1_b", "sieve_m2_b", "sieve_m3_b", "sieve_area_b")


def _apply_fines_gate(rows: list[dict]) -> list[dict]:
    """Порог «Мука с СВ» по варке: мука и рассев считаются с первой пробы, где СВ достигло порога, и ДО КОНЦА варки — даже если на
    подкачке СВ проседает ниже порога (раньше в такие пробы мука не считалась, и линия рвалась). До первого достижения порога
    значения обнуляются. Строки — все пробы по времени (из разных дней), варки режутся теми же правилами, что в boils()."""
    try:
        thr = float(cv_volume.volume_cfg(_volume_cfg_now())["fines_from_sv"])
    except Exception:
        thr = 88.0
    latched, prev = False, None
    for r in rows:
        if prev is None or _is_new_boil(prev, r):
            latched = False
        prev = r
        sv = r.get("sv")
        if not latched and sv is not None and sv >= thr:
            latched = True
        if not latched:
            for k in list(r.keys()):
                if k.startswith(_FINES_KEYS):
                    r[k] = None
    return rows


def trend_range(serial: str, t_from: float, t_to: float, series: Optional[list[str]] = None,
                limit: int = 5000) -> dict:
    """Тренд по реальному времени: пробы с t_from по t_to (epoch, сек) из журнала по дням.
    Возвращает {t:[...], ts:[...], stage:[...], series:{name:[...]}}; не больше limit точек."""
    _hist_backfill(serial)
    series = series or ["small", "medium", "large", "reject", "mean", "median"]
    rows = []
    # перебираем только существующие дни (а не все даты в диапазоне): запрос «с 1970» не падает
    for day in _hist_days(serial):
        try:
            day0 = time.mktime(time.strptime(day, "%Y-%m-%d"))
        except Exception:
            continue
        if day0 > t_to or day0 + 86400 < t_from - 86400:        # день назад — запас: варка могла начаться до окна (нужно для порога СВ)
            continue
        rows.extend(r for r in _hist_read(serial, day) if r.get("t") is not None and r["t"] <= t_to)
    rows.sort(key=lambda r: r["t"])
    _apply_fines_gate(rows)
    bid, prev = 0, None                      # номер варки каждой точки — по тем же правилам, что в журнале (JS границу сам не ищет)
    for r in rows:
        if prev is None or _is_new_boil(prev, r):
            bid += 1
        r["_boil"], prev = bid, r
    rows = [r for r in rows if r["t"] >= t_from]
    if len(rows) > limit:
        rows = rows[-limit:]
    return {
        "from": t_from, "to": t_to,
        "t": [r["t"] for r in rows], "ts": [r["ts"] for r in rows], "boil": [r["_boil"] for r in rows],
        "stage": [r.get("stage") for r in rows],
        "substage": [r.get("substage") for r in rows],
        "series": {s: [(fines_avg(r) if s == "fines_avg" else r.get(s)) for r in rows] for s in series},
    }


EXPORT_COLUMNS = ["ts", "t", "stage", "substage", "sv", "temp", "level", "current", "vac", "cook_time", "seed_age",
                  "count", "mean", "median", "cv_pct", "density", "small", "medium", "large", "reject",
                  "reject_pct", "fines_m1", "fines_m2", "fines_m3", "fines_avg", "fines_area", "fines_n",
                  "good_n", "rej_n", "sieve_b0", "sieve_b1", "sieve_b2", "sieve_b3", "sieve_b4", "sieve_b5", "sieve_b6",
                  "sieve_area_b0", "sieve_area_b1", "sieve_area_b2", "sieve_area_b3", "sieve_area_b4", "sieve_area_b5", "sieve_area_b6",
                  "agg_m1", "agg_m2", "agg_m3", "agg_area", "agg_n", "vtot_m1", "vtot_m2", "vtot_m3", "fines_side_mm", "fines_um", "k_thick", "fines_from_sv",
                  "n_needle", "n_aggregate", "n_crooked", "n_tiny", "n_huge", "suspect",
                  "frac_zones", "frac_pct", "frames"]


def sieve_avg(r: dict, i: int) -> Optional[float]:
    """Фракция рассева i, % объёма — среднее трёх моделей (пропуская пустые)."""
    v = [r["sieve_%s_b%d" % (m, i)] for m in ("m1", "m2", "m3") if r.get("sieve_%s_b%d" % (m, i)) is not None]
    return round(sum(v) / len(v), 3) if v else None


def fines_avg(r: dict) -> Optional[float]:
    """Мелочь по объёму, % — среднее трёх моделей (M1 шар, M2 сфероид, M3 призма); модели без значения пропускаем."""
    v = [r[k] for k in ("fines_m1", "fines_m2", "fines_m3") if r.get(k) is not None]
    return round(sum(v) / len(v), 3) if v else None


def export_rows(serial: str, t_from: Optional[float] = None, t_to: Optional[float] = None) -> list[dict]:
    """Все строки журнала проб (по времени) в заданном диапазоне — для выгрузки в CSV/обучение.
    Без границ — вся история. Колонки — EXPORT_COLUMNS; чего в старой пробе не было — None."""
    _hist_backfill(serial)
    rows = []
    for day in _hist_days(serial):
        rows.extend(r for r in _hist_read(serial, day) if r.get("t") is not None)
    rows.sort(key=lambda r: r["t"])
    _apply_fines_gate(rows)
    rows = [r for r in rows if (t_from is None or r["t"] >= t_from) and (t_to is None or r["t"] <= t_to)]
    def cell(r, c):
        if c == "fines_avg":
            return fines_avg(r)
        if c.startswith("sieve_b"):
            return sieve_avg(r, int(c[7:]))
        return r.get(c)
    return [{c: cell(r, c) for c in EXPORT_COLUMNS} for r in rows]


# --- варки: журнал проб нарезается на варки, по варке — сводка мелочи/сростков/объёма ---
BOIL_GAP_S, BOIL_COOK_DROP_S, BOIL_SV_DROP = db.BOIL_GAP_S, db.BOIL_COOK_DROP_S, db.BOIL_SV_DROP
BOIL_OPEN_S = 15 * 60       # последняя варка «идёт», если последняя проба моложе этого
_is_new_boil = db.is_new_boil


def _wmean(rows: list, key: str, wkey: Optional[str]) -> Optional[float]:
    """Среднее по пробам, где значение есть; с весом wkey (общий объём пробы), если он есть у всех."""
    pts = [(r[key], r.get(wkey) if wkey else None) for r in rows if r.get(key) is not None]
    if not pts:
        return None
    if wkey and all(w for _, w in pts):
        return sum(v * w for v, w in pts) / sum(w for _, w in pts)
    return sum(v for v, _ in pts) / len(pts)


def _vol_block(sel: list) -> dict:
    """Среднее по набору проб (строк журнала): мука, отсеянный брак, рассев (взвешено по общему объёму пробы), сколько проб
    и хороших кристаллов вошло. Строки без значения мелочи пропускаются."""
    def r3(v):
        return None if v is None else round(v, 3)
    fin = [r for r in sel if r.get("fines_m3") is not None]
    out = {"probes": len(fin), "good_n": sum(r.get("good_n") or 0 for r in fin), "rej_n": sum(r.get("rej_n") or 0 for r in fin),
           "ts_from": fin[0]["ts"] if fin else None, "ts_to": fin[-1]["ts"] if fin else None}
    out["fines"] = {m: r3(_wmean(fin, "fines_" + m, "vtot_" + m)) for m in ("m1", "m2", "m3")}
    out["fines"]["area"], out["fines"]["n"] = r3(_wmean(fin, "fines_area", None)), r3(_wmean(fin, "fines_n", None))
    # рассев по объёму (M1–M3, вес — общий объём пробы) и по площади (area, простое среднее по пробам)
    out["sieve"] = {m: [r3(_wmean(fin, "sieve_%s_b%d" % (m, i), ("vtot_" + m) if m != "area" else None)) for i in range(len(cv_volume.SIEVE_MM) + 1)] for m in ("m1", "m2", "m3", "area")}
    return out


def _boil_summary(rows: list, finished: bool, avg_n: int = 4) -> dict:
    """Сводка варки по строкам журнала. Мелочь — по пробам, где она считалась (СВ ≥ порога), с весом по общему
    объёму пробы: большая проба весит больше. Сростки и площадь — по всем пробам варки."""
    def r3(v):
        return None if v is None else round(v, 3)

    out = {"id": rows[0]["ts"], "ts_from": rows[0]["ts"], "ts_to": rows[-1]["ts"],
           "t_from": rows[0]["t"], "t_to": rows[-1]["t"], "n": len(rows), "finished": finished}
    svs = [r["sv"] for r in rows if r.get("sv") is not None]
    out["sv_min"], out["sv_max"] = (min(svs), max(svs)) if svs else (None, None)
    out["counted"] = sum(1 for r in rows if r.get("fines_m3") is not None)   # проб, где мелочь считалась
    fines, agg, vtot = {}, {}, {}
    for m in ("m1", "m2", "m3"):
        w = "vtot_" + m
        fines[m] = r3(_wmean(rows, "fines_" + m, w))
        agg[m] = r3(_wmean(rows, "agg_" + m, w))
        vtot[m] = r3(_wmean(rows, w, None))
    fines["area"], agg["area"] = r3(_wmean(rows, "fines_area", None)), r3(_wmean(rows, "agg_area", None))
    fines["n"], agg["n"] = r3(_wmean(rows, "fines_n", None)), r3(_wmean(rows, "agg_n", None))
    out["fines"], out["agg"], out["vtot"] = fines, agg, vtot
    # рассев по варке — по тем же пробам финиша, что и мука, взвешено по общему объёму пробы
    fin = [r for r in rows if r.get("fines_m3") is not None]
    # рассев по объёму (M1–M3, вес — общий объём пробы) и по площади (area, простое среднее по пробам)
    out["sieve"] = {m: [r3(_wmean(fin, "sieve_%s_b%d" % (m, i), ("vtot_" + m) if m != "area" else None)) for i in range(len(cv_volume.SIEVE_MM) + 1)] for m in ("m1", "m2", "m3", "area")}
    out["all"] = _vol_block(fin)                      # все пробы финиша варки
    out["tail"] = _vol_block(fin[-avg_n:])            # последние N проб финиша (поле «Проб в среднем»)
    last = rows[-1]
    out["cfg"] = {k: last.get(k) for k in ("fines_side_mm", "fines_um", "k_thick", "fines_from_sv", "avg_n")}
    out["cfg"]["avg_n"] = avg_n
    return out


def _group_boils(rows: list[dict]) -> list[list[dict]]:
    """Строки журнала (по времени) → варки: список групп проб."""
    groups: list[list[dict]] = []
    for r in rows:
        if not groups or _is_new_boil(groups[-1][-1], r):
            groups.append([])
        groups[-1].append(r)
    return groups


def boil_groups(serial: str) -> list[list[dict]]:
    """Все варки серийника (старые первыми): группы проб; мука/рассев — с порогом «Мука с СВ» по варке (как в тренде)."""
    _hist_backfill(serial)
    rows, seen = [], set()
    for day in _hist_days(serial):
        for r in _hist_read(serial, day):
            if r.get("t") is not None and r.get("ts") not in seen:
                seen.add(r.get("ts"))
                rows.append(r)
    rows.sort(key=lambda r: r["t"])
    _apply_fines_gate(rows)
    return _group_boils(rows)


def boils(serial: str, limit: int = 6, now: Optional[float] = None) -> list[dict]:
    """Последние варки (новая первой): журнал проб режется на варки (_is_new_boil), по каждой — сводка
    мелочи/сростков/объёма. Последняя варка «идёт», пока последняя проба моложе BOIL_OPEN_S."""
    _hist_backfill(serial)
    avg_n = cv_volume.volume_cfg(_volume_cfg_now())["avg_n"]
    rows, seen = [], set()
    for day in _hist_days(serial):
        for r in _hist_read(serial, day):
            if r.get("t") is not None and r.get("ts") not in seen:
                seen.add(r.get("ts"))
                rows.append(r)
    rows.sort(key=lambda r: r["t"])
    _apply_fines_gate(rows)
    groups = _group_boils(rows)
    now = time.time() if now is None else now
    out = []
    for i, g in enumerate(reversed(groups[-limit:])):
        finished = not (i == 0 and now - g[-1]["t"] <= BOIL_OPEN_S)
        out.append(_boil_summary(g, finished, avg_n))
    return out


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
                keep_last: int = 50, keep_boils: int = 0, ts: Optional[str] = None,
                fracture: Optional[dict] = None, jpeg_quality: int = 85, frame_format: str = "jpg", max_gb: float = 0.0,
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
                png = str(frame_format).lower() == "png"          # PNG — без потерь (≈ 7 МБ на кадр), JPEG — компактно (≈ 1 МБ)
                ov_name = "frame_%d.%s" % (i, "png" if png else "jpg")
                # imencode + write_bytes, а не cv2.imwrite: тот ломается на путях с кириллицей
                ok, enc = (cv2.imencode(".png", images[i], [cv2.IMWRITE_PNG_COMPRESSION, 1]) if png else
                           cv2.imencode(".jpg", images[i], [cv2.IMWRITE_JPEG_QUALITY, int(jpeg_quality)]))
                if not ok:
                    raise OSError("не удалось закодировать кадр в %s" % ("PNG" if png else "JPEG"))
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
        _rotate(serial, keep_last, keep_boils, max_gb)
        log_event("cv_store", "Проба CV сохранена", "info",
                  {"serial": str(serial), "ts": ts, "count": summary.get("count")})
        return {"ts": ts, "dir": str(d), "summary": summary}
    except Exception as e:
        log_event("cv_store", "Ошибка сохранения пробы CV", "error", {"error": str(e)})
        return None


KEEP_MAX_PROBES = 2000      # жёсткий потолок проб в архиве (≈ 10 ГБ), что бы ни стояло в «варок в архиве»


def _boil_starts(serial: str) -> list[float]:
    """Времена начала варок по журналу (по возрастанию) — те же правила, что в boils()."""
    rows = []
    for day in _hist_days(serial):
        rows.extend(r for r in _hist_read(serial, day) if r.get("t") is not None)
    rows.sort(key=lambda r: r["t"])
    starts, prev = [], None
    for r in rows:
        if prev is None or _is_new_boil(prev, r):
            starts.append(r["t"])
        prev = r
    return starts


def _dir_size(p: Path) -> int:
    try:
        return sum(f.stat().st_size for f in p.iterdir() if f.is_file())
    except OSError:
        return 0


def _rotate(serial: str, keep_last: int, keep_boils: int = 0, max_gb: float = 0.0):
    """Архив проб: удаляем только то, что старше И «последних keep_last проб», И начала N-й с конца варки (keep_boils > 0).
    Так кадры последних N варок лежат целиком — их можно просматривать и забирать на разметку; потолок KEEP_MAX_PROBES."""
    sd = _serial_dir(serial)
    if not sd.exists():
        return
    dirs = sorted([p for p in sd.iterdir() if p.is_dir()], key=lambda p: p.name)
    old = dirs[:-keep_last] if keep_last > 0 else []
    if keep_boils > 0 and old:
        starts = _boil_starts(serial)
        if len(starts) < keep_boils:
            old = []                                   # варок меньше, чем «варок в архиве» — удалять нечего
        else:
            t_keep = starts[-keep_boils]
            old = [p for p in old if (_ts_epoch(p.name) or 0) < t_keep]
    if len(dirs) > KEEP_MAX_PROBES:                    # аварийный потолок по диску
        old = list({p.name: p for p in old + dirs[:len(dirs) - KEEP_MAX_PROBES]}.values())
    for p in old:
        shutil.rmtree(p, ignore_errors=True)
    # потолок по занятому месту (max_gb > 0): сверх него удаляем самые старые, но последние keep_last проб не трогаем
    if max_gb and max_gb > 0:
        left = [p for p in dirs if p.exists()]
        sizes = {p: _dir_size(p) for p in left}
        total, cap = sum(sizes.values()), int(max_gb * 1e9)
        protected = set(left[-keep_last:]) if keep_last > 0 else set()
        for p in left:
            if total <= cap:
                break
            if p in protected:
                continue
            total -= sizes[p]
            shutil.rmtree(p, ignore_errors=True)


def _hist_patch(serial: str, ts: str, fields: dict) -> None:
    """Поправить поля одной строки журнала (после ручной правки пробы), остальные строки не трогаем."""
    if _use_db():
        try:
            db.patch_row(_serial_dir(serial).name, ts, fields)
        except Exception as e:
            log_event("cv_store", "Не поправлена строка журнала в SQLite", "warn", {"error": str(e)})
    if not _use_files():
        return
    f = _hist_file(serial, ts)
    with _HIST_LOCK:
        if not f.exists():
            return
        rows, changed = _read_jsonl(f), False
        for r in rows:
            if r.get("ts") == ts:
                r.update(fields)
                changed = True
        if changed:
            tmp = f.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
            tmp.replace(f)


def _clean_frame_path(d: Path, idx: int) -> Optional[Path]:
    for name in ("frame_%d.png", "frame_%d.jpg"):
        p = d / (name % int(idx))
        if p.exists():
            return p
    return None


def edit_fracture(serial: str, ts: str, idx: int = 0, remove=(), promote=(), add=()) -> dict:
    """Ручная правка разломов пробы. remove — id зон (подтверждённых/ручных/кандидатов): «это не разлом»; promote — id
    кандидатов: «это разлом»; add — свои зоны [{poly: [[x, y], …]}] в пикселях кадра. Результат — в result.json пробы
    (зоны, сводка, журнал) и кадр с разметкой — в калибровку разломов (fracture_lab): по ним подбираются пороги."""
    d = _probe_dir(serial, ts)
    if d is None:
        raise FileNotFoundError("проба не найдена")
    rp = d / "result.json"
    r = json.loads(rp.read_text(encoding="utf-8"))
    fr = r.get("fracture") or {}
    zones, cands = [dict(z) for z in fr.get("zones") or []], [dict(z) for z in fr.get("candidates") or []]
    for i, z in enumerate(zones):                              # пробы до правки: id проставляем сами
        z.setdefault("id", "a%d" % i)
        z.setdefault("src", "auto")
    for i, z in enumerate(cands):
        z.setdefault("id", "c%d" % i)
        z.setdefault("src", "candidate")
    fr.setdefault("auto_zones", [dict(z) for z in zones if z["src"] == "auto"])   # исходный автодетект — один раз, для разбора
    rem, prom = {str(x) for x in remove}, {str(x) for x in promote}
    rejected = list(fr.get("rejected") or [])
    for z in zones + cands:
        if z["id"] in rem:
            rejected.append({k: z.get(k) for k in ("id", "src", "bbox", "poly", "conf")})
    zones = [z for z in zones if z["id"] not in rem]
    n_next = 1 + max([int(str(z["id"])[1:]) for z in zones + cands if str(z["id"])[1:].isdigit()] + [-1] +
                     [int(str(z["id"])[1:]) for z in rejected if str(z.get("id", ""))[1:].isdigit()])
    for z in cands:
        if z["id"] in prom:
            z.update({"id": "p%d" % n_next, "src": "promoted"})
            n_next += 1
            zones.append(z)
    cands = [z for z in cands if z["id"] not in rem and z["id"] not in prom and z.get("src") == "candidate"]
    w_h = fr.get("size")
    if not w_h:
        p = _clean_frame_path(d, idx)
        im = cv2.imdecode(np.frombuffer(p.read_bytes(), np.uint8), cv2.IMREAD_COLOR) if p else None
        w_h = [int(im.shape[1]), int(im.shape[0])] if im is not None else None
    for a in add or ():
        pts = [[int(round(float(x))), int(round(float(y)))] for x, y in (a.get("poly") or [])]
        if len(pts) < 3:
            continue
        arr = np.array(pts, np.int32).reshape(-1, 1, 2)
        x, y, bw, bh = cv2.boundingRect(arr)
        zones.append({"id": "m%d" % n_next, "src": "manual", "poly": pts, "bbox": [int(x), int(y), int(bw), int(bh)],
                      "area": int(cv2.contourArea(arr)), "conf": 1.0, "D": None, "T": None})
        n_next += 1
    fr["zones"], fr["candidates"], fr["rejected"], fr["size"], fr["edited"] = zones, cands, rejected, w_h, True
    sm = dict(fr.get("summary") or {})
    sm["zones"], sm["has_fracture"], sm["edited"] = len(zones), bool(zones), True
    area = sum(int(z.get("area") or 0) for z in zones)
    sm["area_pct"] = round(100.0 * area / (w_h[0] * w_h[1]), 2) if w_h else sm.get("area_pct", 0.0)
    fr["summary"] = sm
    r["fracture"] = fr
    tmp = rp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(rp)
    _hist_patch(serial, ts, {"frac_zones": sm["zones"], "frac_pct": sm["area_pct"]})
    if _use_db():
        try:
            now = time.time()
            tag = _serial_dir(serial).name
            for zid in rem:
                db.log_fracture_edit(tag, ts, "remove", zid, idx, now)
            for zid in prom:
                db.log_fracture_edit(tag, ts, "promote", zid, idx, now)
            for _ in add or ():
                db.log_fracture_edit(tag, ts, "add", None, idx, now)
        except Exception as e:
            log_event("cv_store", "Не записана правка разломов в SQLite", "warn", {"error": str(e)})
    try:                                                      # в калибровку: кадр без разметки + что оператор решил
        import fracture_lab
        p = _clean_frame_path(d, idx)
        img = cv2.imdecode(np.frombuffer(p.read_bytes(), np.uint8), cv2.IMREAD_COLOR) if p else None
        if img is not None:
            fracture_lab.save_probe_sample(
                "%s/%s" % (_serial_dir(serial).name, ts), img, "fracture" if zones else "ok",
                {"zones": [{k: z.get(k) for k in ("id", "src", "poly", "bbox", "conf")} for z in zones],
                 "rejected": [{k: z.get(k) for k in ("id", "src", "poly", "bbox", "conf")} for z in rejected], "frame": int(idx)})
    except Exception as e:
        log_event("cv_store", "Правка разломов: кадр не попал в калибровку", "warn", {"error": str(e)})
    return fr


def export_png(serial: str, ts: str, idx: int, dest_dir) -> dict:
    """Сохранить ЧИСТЫЙ кадр пробы (без разметки) в PNG в папку dest_dir — для ручной разметки и дообучения.
    PNG делается из сохранённого JPEG пробы. Пробы со старым форматом (контуры впечатаны в картинку) — отказ."""
    d = _probe_dir(serial, ts)
    if d is None:
        return {"ok": False, "error": "проба не найдена"}
    pp, pj = d / ("frame_%d.png" % int(idx)), d / ("frame_%d.jpg" % int(idx))
    p = pp if pp.exists() else pj
    if not p.exists():
        return {"ok": False, "error": "чистого кадра нет (старая проба с контурами в картинке или кадр удалён)"}
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    name = "%s_%s_f%d.png" % (_serial_dir(serial).name, ts, int(idx))
    if p.suffix == ".png":                              # архив уже в PNG (без потерь) — копируем как есть, без перекодирования
        (dest / name).write_bytes(p.read_bytes())
        return {"ok": True, "name": name, "path": str(dest / name), "size_mb": round(p.stat().st_size / 1e6, 1)}
    img = cv2.imdecode(np.frombuffer(p.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return {"ok": False, "error": "кадр не читается"}
    ok, enc = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 3])     # imencode+write_bytes: путь может быть кириллическим
    if not ok:
        return {"ok": False, "error": "не удалось закодировать PNG"}
    (dest / name).write_bytes(enc.tobytes())
    return {"ok": True, "name": name, "path": str(dest / name), "size_mb": round(len(enc) / 1e6, 1)}


def export_probe_pngs(serial: str, ts: str, dest_dir) -> list[dict]:
    """Все чистые кадры пробы в PNG."""
    d = _probe_dir(serial, ts)
    if d is None:
        return [{"ok": False, "error": "проба не найдена"}]
    idxs = sorted({int(x.stem.split("_")[1]) for x in list(d.glob("frame_*.jpg")) + list(d.glob("frame_*.png")) if x.stem.split("_")[1].isdigit()})
    return [export_png(serial, ts, i, dest_dir) for i in idxs] or [{"ok": False, "error": "в пробе нет чистых кадров"}]


def _volume_cfg_now() -> dict:
    """Настройки объёма из конфига (поля блока «Объём и мелочь»); нет конфига — дефолты."""
    try:
        import plate_config
        cv = plate_config.load().get("cv") or {}
        return {"volume": cv.get("volume") or {}, "reject_always": cv.get("reject_always")}
    except Exception:
        return {}


def _with_volume(d: Path, r: dict, cfg: Optional[dict] = None) -> dict:
    """Проба, снятая до появления объёма (в summary нет volume): досчитать мелочь/сростки по
    сохранённым объектам кадров по ТЕКУЩИМ полям порога и k. Файлы пробы не меняем — только ответ."""
    s = (r or {}).get("summary")
    if not s:
        return r
    cfg = cfg if cfg is not None else _volume_cfg_now()
    sub_code = (r.get("plc") or {}).get("substage")
    old, cur = s.get("volume_cfg") or {}, cv_volume.volume_cfg(cfg, sub_code)
    same = all(old.get(k) == cur.get(k) for k in ("fines_um", "fines_mode", "k_thick", "fines_from_sv", "calc_ver"))
    if s.get("volume") and "sieve" in s["volume"] and same:       # посчитана с теми же полями — не трогаем
        return r
    try:
        from types import SimpleNamespace as NS
        tot = cv_volume.empty_sums()
        fines_on = cv_volume.fines_on(cfg, s.get("sv", r.get("sv")))
        for i in range(len(r.get("frames") or [1])):
            objs = json.loads((d / ("objects_%d.json" % i)).read_text(encoding="utf-8"))
            ms = [NS(group=o.get("group"), defect=o.get("defect"), size_um=o["size_um"],
                     length_um=o["length_um"], width_um=o["width_um"]) for o in objs]
            tot = cv_volume.add_sums(tot, cv_volume.sums_for(ms, cfg, fines_on, sub_code))
        s["volume"], s["volume_pct"] = tot, cv_volume.percents(tot)
        s["volume_cfg"] = cv_volume.volume_cfg(cfg, sub_code)
    except Exception:
        pass            # нет объектов/битый файл — проба просто без объёма
    return r


def recompute_journal(serial: str) -> int:
    """Пересчитать объём/муку/рассев в журнале по ТЕКУЩИМ полям («Мука, мм», «Мука с СВ», k) — для проб, у которых ещё
    сохранены кадры (последние keep_last). Остальные строки журнала остаются как были. Возвращает число обновлённых строк."""
    sd = _serial_dir(serial)
    if not sd.exists():
        return 0
    cfg, fresh = _volume_cfg_now(), {}
    for p in sorted(x for x in sd.iterdir() if x.is_dir()):
        try:
            row = _hist_row(_with_volume(p, json.loads((p / "result.json").read_text(encoding="utf-8")), cfg))
            if row:
                fresh[row["ts"]] = row
        except Exception:
            continue
    n = 0
    if _use_db():
        tag = _serial_dir(serial).name
        cur = {r["ts"]: r for r in db.read_rows(tag)}
        for ts, row in fresh.items():
            if ts in cur and cur[ts] != row:
                db.upsert_row(tag, row)
                n += 1
    if _use_files():
        with _HIST_LOCK:
            nf = 0
            for f in (sorted(_hist_dir(serial).glob("*.jsonl")) if _hist_dir(serial).exists() else []):
                rows, changed = _read_jsonl(f), False
                for i, r in enumerate(rows):
                    if r.get("ts") in fresh and fresh[r["ts"]] != r:
                        rows[i], changed = fresh[r["ts"]], True
                        nf += 1
                if changed:
                    tmp = f.with_suffix(".jsonl.tmp")
                    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
                    tmp.replace(f)
            n = max(n, nf)
    return n


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
            out.append({"ts": r["ts"], "stage": r.get("stage"), "substage": (r.get("plc") or {}).get("substage"), "sv": r.get("sv"),
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
    for name in ("frame_%d.png", "frame_%d.jpg", "overlay_%d.jpg", "overlay_%d.png"):
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
