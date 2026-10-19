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
HISTORY = DATA_DIR / "cv_history"      # старый журнал проб (jsonl, до 1.9.0): теперь только разовый импорт в SQLite
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
    fl = [x for x in (vp.get("flat") or {}).values() if x is not None]
    out["fines_flat"] = round(sum(fl) / len(fl), 3) if fl else None      # мука без плавного порога, % (среднее M1–M3); нет кадров — None
    for key in ("m1", "m2", "m3"):
        tot = (sums.get(key) or {}).get("total")
        out["vtot_" + key] = round(tot / 1e9 / n_frames, 4) if tot else None
    sv = (vp.get("sieve") or {})
    for mod in ("m1", "m2", "m3", "area"):           # рассев по ситам, %: sieve_<m1|m2|m3|area>_b0 (дно) … b6 (>1,2 мм); area — доля по площади
        for i in range(len(cv_volume.SIEVE_MM) + 1):
            out["sieve_%s_b%d" % (mod, i)] = (sv.get(mod) or [None] * (len(cv_volume.SIEVE_MM) + 1))[i]
    fv = (vp.get("sieve_fine") or {})
    for mod in ("m1", "m2", "m3", "area"):           # мелкий рассев в процентах: fsieve_<m1|m2|m3|area>_b0 (до 0,05 мм) … b20 (больше 1 мм)
        if fv.get(mod):
            for i in range(cv_volume.FINE_N):
                out["fsieve_%s_b%d" % (mod, i)] = fv[mod][i]
    out["good_n"], out["rej_n"] = (sums.get("n") or {}).get("total"), (sums.get("n") or {}).get("agg")     # хороших кристаллов / отсеянного брака в пробе (штук, все кадры)
    out["fines_side_mm"], out["fines_um"] = cfg.get("fines_side_mm"), cfg.get("fines_um")
    out["k_thick"], out["fines_from_sv"] = cfg.get("k_thick"), cfg.get("fines_from_sv")
    return out


PAUSE_STAGES = (20, 21)     # УНВ / пауза: приходят посреди роста, это не этап рецепта — фазу не обрывают, подкачки/росты не считают


def phases_for_boil(stages: list) -> list:
    """Фаза каждой пробы варки по ПОРЯДКУ стадий (рецепт идёт строго по порядку): стадия 5 = подкачка, 7 = рост. 1-я «5» → p1,
    1-я «7» → g1, 2-я «5» → p2, 2-я «7» → g2 (3-я и далее вливаются во 2-ю). Прочие стадии (сгущение, затравка, уваривание…) — None.
    УНВ / пауза (PAUSE_STAGES) посреди роста — как у предыдущей пробы: рост 7 → 20 → 7 остаётся одним ростом.
    Стадия пробы — на момент её начала: проба, начатая в подкачке, остаётся в подкачке, даже если к концу стадия сменилась."""
    pump = grow = 0
    last, prev, out = None, None, []
    for st in stages:
        if st is None or int(st) in PAUSE_STAGES:
            out.append(prev)                                   # стадия неизвестна или УНВ/пауза — как у предыдущей пробы
            continue
        st = int(st)
        if st == 5:
            if last != 5:
                pump += 1
            prev = "p%d" % min(pump, 2)
        elif st == 7:
            if last != 7:
                grow += 1
            prev = "g%d" % min(grow, 2)
        else:
            prev = None
        out.append(prev)
        last = st
    return out


_PHASE_CACHE: dict = {}


def phase_map(serial: str, max_age: float = 30.0) -> dict:
    """{ts: фаза} по журналу: для проб, снятых до появления фаз (в result.json её нет). Кэш на max_age секунд."""
    tag = _serial_dir(serial).name
    now = time.time()
    hit = _PHASE_CACHE.get(tag)
    if hit and now - hit[0] < max_age:
        return hit[1]
    out = {}
    for g in _group_boils(sorted(db.read_rows(tag), key=lambda r: r["t"])):
        for r, ph in zip(g, phases_for_boil([r.get("stage") for r in g])):
            out[r["ts"]] = ph
    _PHASE_CACHE[tag] = (now, out)
    return out


def phase_state(serial: str, max_idle_s: float = 20 * 60) -> dict:
    """Состояние счётчиков фаз ИДУЩЕЙ варки по журналу (для перезапуска приложения посреди варки): {"pump": n, "grow": n, "last": stage}.
    Последняя проба старше max_idle_s — варка закончилась, счётчики с нуля.
    Ещё: seed_ts — момент заводки (epoch, по времени «с заводки» проб варки) и boil_t_s — время с заводки до «готовности», если она уже была:
    после перезапуска приложения посреди варки отсчёт не теряется."""
    tag = _serial_dir(serial).name
    rows = sorted(db.read_rows(tag, t_from=time.time() - 12 * 3600), key=lambda r: r["t"])
    if not rows or time.time() - rows[-1]["t"] > max_idle_s:
        return {"pump": 0, "grow": 0, "last": None, "seed_ts": None, "boil_t_s": None}
    g = _group_boils(rows)[-1]
    seed_ts = _est_seed_ts(g)
    boil_t_s = next((r["seed_age"] for r in g if r.get("stage") == 9 and r.get("seed_age")), None)
    pump = grow = 0
    last = None
    for r in g:
        st = r.get("stage")
        if st is None or st in PAUSE_STAGES:
            continue
        if st == 5 and last != 5:
            pump += 1
        elif st == 7 and last != 7:
            grow += 1
        last = st
    return {"pump": pump, "grow": grow, "last": last, "seed_ts": seed_ts, "boil_t_s": boil_t_s}


def _est_seed_ts(group: list) -> Optional[float]:
    """Момент заводки варки (epoch): медиана «время пробы − время с заводки» по её пробам, где оно записано; нет таких — None."""
    v = sorted(r["t"] - r["seed_age"] for r in group if r.get("seed_age") is not None and r.get("t") is not None)
    return v[len(v) // 2] if v else None


def fill_seed_age(rows: list) -> dict:
    """{ts: секунд с заводки} для проб, у которых его нет (приложение перезапускали посреди варки — заводка в памяти потерялась), а в варке оно
    у других проб есть: заводку восстанавливаем по ним. Только пробы после заводки (стадия 4–9), строки не меняем."""
    out = {}
    for g in _group_boils(sorted(rows, key=lambda r: r["t"])):
        st = _est_seed_ts(g)
        if st is None:
            continue
        for r in g:
            if r.get("seed_age") is None and r.get("stage") is not None and 4 <= r["stage"] <= 9 and r["t"] >= st - 1:
                out[r["ts"]] = max(0, int(r["t"] - st))
    return out


def finish_probes(fin: list, fin_sv: float = 2.0) -> tuple[list, Optional[float]]:
    """Пробы финиша: СВ в пределах fin_sv (последние ~2 СВ) от конечного. Конечное СВ — медиана СВ последних 5 проб с мукой
    (одиночный выброс или провал СВ на подкачке не сдвигает отсчёт). Нет СВ — последние 4 пробы."""
    svs = [r["sv"] for r in fin[-5:] if r.get("sv") is not None and r["sv"] >= 5]
    if not svs:
        return fin[-4:], None
    ref = sorted(svs)[len(svs) // 2]
    sel = [r for r in fin if r.get("sv") is not None and r["sv"] >= ref - fin_sv]
    return (sel or fin[-4:]), ref


def _model_tag(det: Optional[dict]) -> Optional[str]:
    """«26x_1280.pt@1280» из паспорта модели пробы; старые пробы без паспорта — None."""
    if not det or not det.get("name"):
        return None
    name = str(det["name"]).split(":", 1)[-1]
    return "%s@%s" % (name, det["imgsz"]) if det.get("imgsz") else name


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
        "ts": result["ts"], "t": t, "stage": result.get("stage"), "phase": result.get("phase"), "sv": result.get("sv"),
        # причины брака (среднее число на кадр), разброс размера и плотность — для разбора слипания и обучения
        "n_needle": rs.get("needle"), "n_aggregate": rs.get("aggregate"), "n_crooked": rs.get("crooked"),
        "n_tiny": rs.get("tiny"), "n_huge": rs.get("huge"), "suspect": s.get("suspect"),
        "reject_pct": s.get("reject_pct"), "cv_pct": sz.get("cv_pct"), "density": s.get("density_per_mm2"),
        # режим варки на момент пробы (ПЛК): для разбора «почему слиплось» по серии проб
        "temp": plc.get("temp_app"), "level": plc.get("level"), "current": plc.get("current"),
        "vac": plc.get("press_top"), "cook_time": plc.get("cook_time"), "substage": plc.get("substage"), "seed_age": plc.get("seed_age_s"),
        # клапаны подачи (обратка %, среднее по кадрам) и доза с прошлой пробы (%·с, секунд открыт), номер рецепта
        "v_syrup": plc.get("v_syrup"), "v_water": plc.get("v_water"), "recipe": plc.get("recipe"),
        "syrup_dose": plc.get("syrup_dose"), "syrup_s": plc.get("syrup_s"), "water_dose": plc.get("water_dose"), "water_s": plc.get("water_s"),
        # чем снята проба: имя модели и imgsz — пробы разных моделей сравнивать нельзя (абсолютные цифры мелочи сдвигаются)
        "model": _model_tag(result.get("detector")),
        "count": s.get("count"), "mean": sz.get("mean"), "median": sz.get("median"),
        "size_min": sz.get("min"), "size_max": sz.get("max"), "len_max": sz.get("len_max"),
        "small": pct.get("small"), "medium": pct.get("medium"),
        "large": pct.get("large"), "reject": pct.get("reject"),
        # мелочь и сростки по объёму (модели M1 шар / M2 сфероид / M3 призма) и по площади, % от общего
        **_volume_cols(s, vp, len(result.get("frames") or []) or 1),
        "frac_zones": fr.get("zones"), "frac_pct": fr.get("area_pct"),
        "frames": len(result.get("frames") or []),
    }


def _hist_append(serial: str, result: dict):
    """Строка журнала пробы → в базу (одна короткая транзакция в db.upsert_row)."""
    row = _hist_row(result)
    if row is None:
        return
    try:
        db.upsert_row(_serial_dir(serial).name, row)
    except Exception as e:
        log_event("cv_store", "Не записана строка журнала в SQLite", "warn", {"error": str(e)})


_null_sv_outliers = db.null_sv_outliers      # правило сбойного СВ — в db.py (там же считается boil_id)


def _hist_days(serial: str) -> list[str]:
    """Даты (YYYY-MM-DD), за которые есть пробы в журнале, по возрастанию."""
    return db.days(_serial_dir(serial).name)


def _hist_read(serial: str, day: str, clean: bool = True) -> list[dict]:
    rows = db.read_rows(_serial_dir(serial).name, day=day)
    return _null_sv_outliers(rows) if clean else rows       # clean=False — сырые строки (для перезаписи журнала)


def import_legacy_journal(serial: str) -> int:
    """Разовый импорт старого журнала (cv_history/<serial>/*.jsonl, до версии 1.9.0) в SQLite. Идемпотентно: ключ (serial, ts);
    нет папки со старым журналом — ничего не делает. Файлы не трогает. Возвращает число добавленных проб."""
    tag = _serial_dir(serial).name
    hd = _hist_dir(serial)
    if not hd.exists():
        return 0
    have, rows = db.known_ts(tag), []
    for f in sorted(hd.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue            # битая строка (обрыв записи) не рушит весь файл
            if r.get("ts") not in have and r.get("t") is not None:
                have.add(r["ts"])
                rows.append(r)
    return db.upsert_many(tag, rows) if rows else 0


def _hist_backfill(serial: str):
    """Один раз за запуск: добавить в журнал пробы, которые уже лежат в cv_results (до появления
    журнала). Идемпотентно: что уже есть в журнале — не дублируем. Заодно импортируется старый журнал jsonl."""
    if serial in _HIST_FILLED:
        return
    _HIST_FILLED.add(serial)
    try:
        import_legacy_journal(serial)       # старые jsonl (версии до 1.9.0) → база, один раз
    except Exception as e:
        log_event("cv_store", "Не удалось импортировать старый журнал в SQLite", "warn", {"error": str(e)})
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


_FINES_KEYS = ("fsieve_", "fines_m1", "fines_m2", "fines_m3", "fines_area", "fines_n",
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
        if not latched and not r.get("phase"):                # на фазе подкачка/рост мука нужна с самого начала — порог СВ её не гасит
            for k in list(r.keys()):
                if k.startswith(_FINES_KEYS):
                    r[k] = None
    return rows


def _trend_val(r: dict, name: str):
    if name == "fines_avg":
        return fines_avg(r)
    if name == "fines_flat":
        return r.get("_fines_flat")
    if name.startswith("fsv") and name[3:].isdigit() and int(name[3:]) < len(FSV_GROUPS):
        return fsieve_group(r, int(name[3:]))
    return r.get(name)


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
    for r in rows:                                   # плоская мука — ДО порога СВ (гейт обнуляет fsieve_*): видно всю варку с начала
        r["_fines_flat"] = fines_flat(r)
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
        "fines_um": [r.get("fines_um") for r in rows],
        "phase": [r.get("phase") for r in rows],               # порог муки пробы (диаметр, мкм) — с учётом её подстадии
        "stage": [r.get("stage") for r in rows],
        "substage": [r.get("substage") for r in rows],
        "series": {s: [_trend_val(r, s) for r in rows] for s in series},
    }


EXPORT_COLUMNS = ["ts", "t", "stage", "substage", "phase", "sv", "temp", "level", "current", "vac", "cook_time", "seed_age",
                  "count", "mean", "median", "size_min", "size_max", "len_max", "cv_pct", "density", "small", "medium", "large", "reject",
                  "reject_pct", "fines_m1", "fines_m2", "fines_m3", "fines_avg", "fines_flat", "fines_area", "fines_n",
                  "good_n", "rej_n", "sieve_b0", "sieve_b1", "sieve_b2", "sieve_b3", "sieve_b4", "sieve_b5", "sieve_b6",
                  "sieve_area_b0", "sieve_area_b1", "sieve_area_b2", "sieve_area_b3", "sieve_area_b4", "sieve_area_b5", "sieve_area_b6",
                  "agg_m1", "agg_m2", "agg_m3", "agg_area", "agg_n", "vtot_m1", "vtot_m2", "vtot_m3", "fines_side_mm", "fines_um", "k_thick", "fines_from_sv",
                  "n_needle", "n_aggregate", "n_crooked", "n_tiny", "n_huge", "suspect",
                  "frac_zones", "frac_pct", "frames",
                  "v_syrup", "v_water", "syrup_dose", "syrup_s", "water_dose", "water_s", "recipe", "model"]


def sieve_avg(r: dict, i: int) -> Optional[float]:
    """Фракция рассева i, % объёма — среднее трёх моделей (пропуская пустые)."""
    v = [r["sieve_%s_b%d" % (m, i)] for m in ("m1", "m2", "m3") if r.get("sieve_%s_b%d" % (m, i)) is not None]
    return round(sum(v) / len(v), 3) if v else None


# серии рассева для тренда: fsv0…fsv8 — доли по объёму (среднее M1–M3): диапазон муки по 0,05 мм (fsv0–3: 0–0,05 … 0,15–0,20),
# дальше 0,20–0,40 / 0,40–0,60 / 0,60–0,80 / 0,80–1,00 / >1,0 мм (fsv4–8)
FSV_GROUPS = [range(i, i + 1) for i in range(4)] + [range(4, 8), range(8, 12), range(12, 16), range(16, 20), range(20, 21)]


def fsieve_total(r: dict, i0: int, i1: int) -> Optional[float]:
    """Сумма корзин мелкого рассева i0..i1-1, % объёма (каждая — среднее трёх моделей). Нет данных — None."""
    vals = []
    for i in range(i0, i1):
        v = [r["fsieve_%s_b%d" % (m, i)] for m in ("m1", "m2", "m3") if r.get("fsieve_%s_b%d" % (m, i)) is not None]
        if v:
            vals.append(sum(v) / len(v))
    return round(sum(vals), 3) if vals else None


def fsieve_group(r: dict, k: int) -> Optional[float]:
    """Доля группы k мелкого рассева пробы, % объёма: сумма фракций по 0,05 мм, каждая — среднее трёх моделей. Нет рассева — None."""
    return fsieve_total(r, FSV_GROUPS[k].start, FSV_GROUPS[k].stop)


def fines_flat(r: dict) -> Optional[float]:
    """Мука БЕЗ плавного порога, % объёма: все хорошие кристаллы мельче общего порога из поля «Мука» (0,2 мм по стороне = 226 мкм по диаметру),
    без масштаба фазы и кривой — один порог на всю варку. Значение посчитано при разборе пробы (cv_volume, «flat»), среднее M1–M3."""
    return r.get("fines_flat")


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


CI_MIN_PROBES = 3      # интервал муки: меньше проб — не показываем (при 2 пробах t = 12,7, интервал ни о чём)
# t Стьюдента, 97,5 % (двусторонний 95 %), по числу степеней свободы 1…30; дальше — 1,96 + 2,4/df (точность ~0,005)
_T975 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
         2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042)


def _t975(df: int) -> float:
    return _T975[df - 1] if df <= len(_T975) else 1.96 + 2.4 / df


def _wmean_ci(rows: list, key: str, wkey: str) -> tuple[Optional[float], Optional[float]]:
    """95% интервал взвешенного среднего (как _wmean): среднее ± t·SE, t Стьюдента с n−1 степенями свободы — на малом числе проб
    честно широкий (бутстрэп там занижал в разы). SE взвешенного среднего: √(n/(n−1)·Σw²(v−m)²)/Σw, при равных весах это σ/√n.
    Мука в процентах — интервал обрезаем 0…100. Меньше CI_MIN_PROBES проб — (None, None)."""
    pts = [(r[key], r.get(wkey)) for r in rows if r.get(key) is not None]
    n = len(pts)
    if n < CI_MIN_PROBES:
        return None, None
    v = np.array([p[0] for p in pts], dtype=float)
    w = np.array([p[1] for p in pts], dtype=float) if all(p[1] for p in pts) else np.ones(n)
    m = float((v * w).sum() / w.sum())
    se = float(np.sqrt(n / (n - 1) * (w * w * (v - m) ** 2).sum()) / w.sum())
    h = _t975(n - 1) * se
    return max(0.0, m - h), min(100.0, m + h)


def _vol_block(sel: list) -> dict:
    """Среднее по набору проб (строк журнала): мука, отсеянный брак, рассев (взвешено по общему объёму пробы), сколько проб
    и хороших кристаллов вошло. Строки без значения мелочи пропускаются."""
    def r3(v):
        return None if v is None else round(v, 3)
    fin = [r for r in sel if r.get("fines_m3") is not None]
    out = {"probes": len(fin), "good_n": sum(r.get("good_n") or 0 for r in fin), "rej_n": sum(r.get("rej_n") or 0 for r in fin),
           "ts_from": fin[0]["ts"] if fin else None, "ts_to": fin[-1]["ts"] if fin else None}
    svs = [r["sv"] for r in fin if r.get("sv") is not None]
    out["sv_from"], out["sv_to"] = (min(svs), max(svs)) if svs else (None, None)     # в каком диапазоне СВ сняты пробы этого рассева
    out["fines"] = {m: r3(_wmean(fin, "fines_" + m, "vtot_" + m)) for m in ("m1", "m2", "m3")}
    out["fines"]["area"], out["fines"]["n"] = r3(_wmean(fin, "fines_area", None)), r3(_wmean(fin, "fines_n", None))
    # 95% интервал муки по моделям (t-оценка по пробам): разница двух варок меньше интервала — шум пробы, а не процесс
    ci = {m: _wmean_ci(fin, "fines_" + m, "vtot_" + m) for m in ("m1", "m2", "m3")}
    out["fines_lo"] = {m: r3(ci[m][0]) for m in ci}
    out["fines_hi"] = {m: r3(ci[m][1]) for m in ci}
    # рассев по объёму (M1–M3, вес — общий объём пробы) и по площади (area, простое среднее по пробам)
    out["sieve"] = {m: [r3(_wmean(fin, "sieve_%s_b%d" % (m, i), ("vtot_" + m) if m != "area" else None)) for i in range(len(cv_volume.SIEVE_MM) + 1)] for m in ("m1", "m2", "m3", "area")}
    out["fsieve"] = _fsieve(fin)
    return out


def _fsieve(rows: list) -> dict:
    """Мелкий рассев по набору проб: доля каждой фракции (0,05 мм) по моделям M1–M3 (вес — общий объём пробы) и по площади, % — списки по 21 фракции.
    Нет ни одной пробы с мелким рассевом (старые пробы без кадров) — None."""
    def r3(v):
        return None if v is None else round(v, 3)
    if not any(r.get("fsieve_m3_b0") is not None for r in rows):
        return None
    return {m: [r3(_wmean(rows, "fsieve_%s_b%d" % (m, i), ("vtot_" + m) if m != "area" else None)) for i in range(cv_volume.FINE_N)] for m in ("m1", "m2", "m3", "area")}


def phase_durations(rows: list) -> dict:
    """{фаза: секунд} по пробам варки (строки по времени). Фаза идёт от первой её пробы до первой пробы следующей (последняя проба
    варки — до неё самой); несколько отрезков одной фазы (3-я и далее подкачка вливаются во 2-ю) складываются."""
    out: dict = {}
    i, n = 0, len(rows)
    while i < n:
        ph = rows[i].get("phase")
        j = i
        while j + 1 < n and rows[j + 1].get("phase") == ph:
            j += 1
        if ph:
            end = rows[j + 1]["t"] if j + 1 < n else rows[j]["t"]
            out[ph] = out.get(ph, 0.0) + max(0.0, end - rows[i]["t"])
        i = j + 1
    return {k: round(v) for k, v in out.items()}


def _boil_summary(rows: list, finished: bool, avg_n: int = 4) -> dict:
    """Сводка варки по строкам журнала. Мелочь — по пробам, где она считалась (СВ ≥ порога), с весом по общему
    объёму пробы: большая проба весит больше. Сростки и площадь — по всем пробам варки."""
    def r3(v):
        return None if v is None else round(v, 3)

    out = {"id": rows[0]["ts"], "ts_from": rows[0]["ts"], "ts_to": rows[-1]["ts"],
           "t_from": rows[0]["t"], "t_to": rows[-1]["t"], "n": len(rows), "finished": finished}
    svs = [r["sv"] for r in rows if r.get("sv") is not None]
    out["sv_min"], out["sv_max"] = (min(svs), max(svs)) if svs else (None, None)
    # итоги варки (мука/рассев «по варке», финиш) — по пробам БЕЗ фазы (сгущение, затравка, уваривание…): на фазах подкачка/рост
    # сита и порог муки в масштабе фазы, их смешивать с финишем нельзя (они идут отдельными блоками phases). Нет таких проб — берём все.
    base = [r for r in rows if not r.get("phase")] or rows
    out["counted"] = sum(1 for r in base if r.get("fines_m3") is not None)   # проб, где мелочь считалась
    fines, agg, vtot = {}, {}, {}
    for m in ("m1", "m2", "m3"):
        w = "vtot_" + m
        fines[m] = r3(_wmean(base, "fines_" + m, w))
        agg[m] = r3(_wmean(base, "agg_" + m, w))
        vtot[m] = r3(_wmean(base, w, None))
    fines["area"], agg["area"] = r3(_wmean(base, "fines_area", None)), r3(_wmean(base, "agg_area", None))
    fines["n"], agg["n"] = r3(_wmean(base, "fines_n", None)), r3(_wmean(base, "agg_n", None))
    out["fines"], out["agg"], out["vtot"] = fines, agg, vtot
    out["size"] = size_extremes(rows)                 # крайние кристаллы всей варки (все пробы, без брака/сростков)
    # рассев по варке — по тем же пробам финиша, что и мука, взвешено по общему объёму пробы
    fin = [r for r in base if r.get("fines_m3") is not None]
    # рассев по объёму (M1–M3, вес — общий объём пробы) и по площади (area, простое среднее по пробам)
    out["sieve"] = {m: [r3(_wmean(fin, "sieve_%s_b%d" % (m, i), ("vtot_" + m) if m != "area" else None)) for i in range(len(cv_volume.SIEVE_MM) + 1)] for m in ("m1", "m2", "m3", "area")}
    out["fsieve"] = _fsieve(fin)
    out["all"] = _vol_block(fin)                      # все пробы финиша варки
    out["tail"] = _vol_block(finish_probes(fin)[0])   # финиш: пробы последних ~2 СВ (раньше — последние N проб)
    # по фазам (подкачка 1 / рост 1 / подкачка 2 / рост 2): ВСЕ пробы фазы, мука и рассев в масштабе фазы
    out["phases"] = {}
    out["dur_s"] = round(rows[-1]["t"] - rows[0]["t"])                       # вся варка по пробам, с
    durs = phase_durations(rows)
    for ph in cv_volume.PHASES:
        rp = [r for r in rows if r.get("phase") == ph]
        if rp:
            blk = _vol_block(rp)
            blk["size"] = size_extremes(rp)
            blk.update({"n": len(rp), "ts_from": rp[0]["ts"], "ts_to": rp[-1]["ts"], "title": cv_volume.PHASE_TITLES[ph], "dur_s": durs.get(ph)})
            out["phases"][ph] = blk
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


def boils(serial: str, limit: int = 6, now: Optional[float] = None, offset: int = 0) -> list[dict]:
    """Последние варки (новая первой): журнал проб режется на варки (_is_new_boil), по каждой — сводка
    мелочи/сростков/объёма. Последняя варка «идёт», пока последняя проба моложе BOIL_OPEN_S.
    offset — сколько самых новых варок пропустить (листание вглубь истории страницами по limit)."""
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
    for i, g in enumerate(list(reversed(groups))[offset:offset + limit], start=offset):
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


def _ext(fn, vals: list) -> Optional[float]:
    """min/max по значениям, где они есть; нет ни одного (старые пробы, пустой кадр) — None."""
    v = [x for x in vals if x is not None]
    return fn(v) if v else None


def size_extremes(rows: list) -> dict:
    """Самый мелкий и крупный кристалл (диаметр) и самый длинный (длинная сторона), мкм, по строкам журнала; нет данных — None."""
    return {"min": _ext(min, [r.get("size_min") for r in rows]), "max": _ext(max, [r.get("size_max") for r in rows]),
            "len_max": _ext(max, [r.get("len_max") for r in rows])}


def _extreme_at(frames: list[dict]) -> dict:
    """Где в пробе самый крупный хороший кристалл и самый большой сросток: [номер кадра, номер объекта в objects_N.json]
    (плитки в окне CV кликабельны — переход на кадр и подсветка). Самый большой сросток — ещё и его размер."""
    out = {}
    for key, at in (("max", "max_at"), ("agg_max", "agg_max_at")):
        best = None
        for k, f in enumerate(frames):
            su = (f.get("summary") or {}).get("size_um") or {}
            v, i = su.get(key), su.get(key + "_i") if key == "agg_max" else su.get("max_i")
            if v is not None and i is not None and (best is None or v > best[0]):
                best = (v, [int(f.get("idx", k)), int(i)])
        if best:
            out[at] = best[1]
            if key == "agg_max":
                out["agg_max"] = best[0]
    return out


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
        **_extreme_at(frames),
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
            # крайние значения не усредняем, а берём по всем кадрам пробы
            "min": _ext(min, [s["size_um"].get("min") for s in ok]),
            "max": _ext(max, [s["size_um"].get("max") for s in ok]),
            "len_max": _ext(max, [s["size_um"].get("len_max") for s in ok]),
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
                plc: Optional[dict] = None, phase: Optional[str] = None,
                detector: Optional[dict] = None, infer: Optional[dict] = None) -> Optional[dict]:
    """Сохранить пробу. frames — список {file, summary, objects}. images — ЧИСТЫЕ кадры пробы
    (numpy BGR), пишутся в JPEG (jpeg_quality); контуры поверх рисует браузер по objects_N.json.
    thumb_img — кадр с контурами для миниатюры (нет — миниатюра из чистого кадра 0).
    detector — паспорт модели от сайдкара (name, sha, train_date, train_imgsz, imgsz…),
    infer — параметры прогона (conf, iou, tiles, overlap, seam_refine). Нет — ключей в result.json нет.

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
            "phase": phase,                                  # фаза варки на начало пробы: p1/g1/p2/g2 или None (см. phases_for_boil)
            "sv": round(sv, 1) if sv is not None else None,   # СВ пробы (от него зависит учёт брака)
            # режим варки из ПЛК на момент пробы: temp_app °C, level %, current A (ток циркулятора),
            # press_top (разрежение сверху), cook_time с, seed_age_s с (время с заводки). Нет ПЛК — ключа нет.
            **({"plc": plc} if plc else {}),
            **({"detector": detector} if detector else {}),     # чем снята проба: модель и её imgsz
            **({"infer": infer} if infer else {}),              # как: conf / iou / tiles / overlap
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
    try:
        db.patch_row(_serial_dir(serial).name, ts, fields)
    except Exception as e:
        log_event("cv_store", "Не поправлена строка журнала в SQLite", "warn", {"error": str(e)})


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


def _with_volume(d: Path, r: dict, cfg: Optional[dict] = None, phase: Optional[str] = None, boil_t_s: Optional[float] = None,
                 seed_age_s: Optional[float] = None, force: bool = False) -> dict:
    """Проба, снятая до появления объёма/фаз (или с другими полями порога): досчитать муку, сростки и рассев по сохранённым
    объектам кадров по ТЕКУЩИМ полям (порог, k, масштаб фазы). Файлы пробы не меняем — только ответ. Фаза — из result.json,
    а для старых проб — по порядку стадий в журнале (phase_map)."""
    s = (r or {}).get("summary")
    if not s:
        return r
    cfg = cfg if cfg is not None else _volume_cfg_now()
    phase = phase or r.get("phase") or phase_map(d.parent.name).get(r.get("ts"))
    own_age = (r.get("plc") or {}).get("seed_age_s")           # время с заводки на начало пробы — для плавного порога муки
    if own_age is not None:
        seed_age_s = own_age
    elif seed_age_s is not None:                                # в пробе его нет (перезапуск посреди варки) — восстановленное по варке
        r["plc"] = {**(r.get("plc") or {}), "seed_age_s": seed_age_s}
    old = s.get("volume_cfg") or {}
    if boil_t_s is None:
        boil_t_s = old.get("fines_curve_boil_t_s")                 # растяжение варки уже определено — при показе пробы его не теряем
    cur = cv_volume.volume_cfg(cfg, phase, seed_age_s, boil_t_s)
    same = all(old.get(k) == cur.get(k) for k in ("fines_um", "fines_mode", "k_thick", "fines_from_sv", "calc_ver", "phase"))
    same = same and bool(old.get("fines_curve_used")) == bool(cur.get("fines_curve_used"))      # у старых проб ключа нет = порог фазы
    same = same and not force
    if s.get("volume") and "sieve" in s["volume"] and "sieve_fine" in s["volume"] and same:       # посчитана с теми же полями — не трогаем
        return r
    try:
        from types import SimpleNamespace as NS
        tot = cv_volume.empty_sums()
        fines_on = cv_volume.fines_on(cfg, s.get("sv", r.get("sv"))) or phase is not None
        for i in range(len(r.get("frames") or [1])):
            f = d / ("objects_%d.json" % i)
            objs = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []     # кадр без кристаллов файла не пишет (save_sample) — это пустой кадр, а не ошибка
            ms = [NS(group=o.get("group"), defect=o.get("defect"), size_um=o["size_um"],
                     length_um=o["length_um"], width_um=o["width_um"]) for o in objs]
            tot = cv_volume.add_sums(tot, cv_volume.sums_for(ms, cfg, fines_on, phase, seed_age_s, boil_t_s))
        s["volume"], s["volume_pct"] = tot, cv_volume.percents(tot)
        s["volume_cfg"] = cv_volume.volume_cfg(cfg, phase, seed_age_s, boil_t_s)
        if phase:
            r["phase"] = phase
    except Exception as e:      # нет объектов/битый файл — проба остаётся как была; раньше молча, из-за чего «застрявшую» пробу не найти
        log_event("cv_store", "Проба не пересчитана по кадрам", "warn", {"ts": r.get("ts"), "error": repr(e)})
    return r


def boil_fit_times(rows: list, now: Optional[float] = None) -> dict:
    """{ts пробы: секунд с заводки до «готовности» ЕЁ варки}. Время берём по первой пробе стадии 9 («готовность»); нет такой, а варка уже закрыта
    (не последняя «идущая») — по её последней пробе. Идущая варка без «готовности» — не попадает в словарь (считается по средней кривой).
    Нужен seed_age пробы (время с заводки); без него (приложение перезапускали посреди варки) варку не растягиваем."""
    now = time.time() if now is None else now
    out = {}
    fill = fill_seed_age(rows)
    rows = [dict(r, seed_age=fill[r["ts"]]) if r["ts"] in fill else r for r in rows]       # потерянное «с заводки» восстановлено по соседним пробам
    groups = _group_boils(sorted(rows, key=lambda r: r["t"]))
    for gi, g in enumerate(groups):
        t9 = next((r["seed_age"] for r in g if r.get("stage") == 9 and r.get("seed_age")), None)
        if t9 is None:
            closed = gi < len(groups) - 1 or now - g[-1]["t"] > BOIL_OPEN_S
            last = next((r["seed_age"] for r in reversed(g) if r.get("seed_age")), None)
            t9 = last if closed else None
        if t9:
            for r in g:
                out[r["ts"]] = t9
    return out


def refit_curve(serial: str, hours: float = 12.0) -> int:
    """Растянуть плавный порог муки под настоящую длительность варок за последние hours часов и пересчитать их пробы. Только если плавный порог
    включён. Вызывается автоматически: при первой пробе «готовности» и при начале новой варки (закрылась предыдущая)."""
    if not cv_volume.volume_cfg(_volume_cfg_now())["fines_curve_on"]:
        return 0
    return recompute_journal(serial, t_from=time.time() - hours * 3600)


def recompute_journal(serial: str, t_from: Optional[float] = None) -> int:
    """Пересчитать объём/муку/рассев в журнале по ТЕКУЩИМ полям («Мука, мм», «Мука с СВ», k) — для проб, у которых ещё
    сохранены кадры (последние keep_last). Остальные строки журнала остаются как были. Возвращает число обновлённых строк."""
    sd = _serial_dir(serial)
    if not sd.exists():
        return 0
    cfg, fresh = _volume_cfg_now(), {}
    tag = _serial_dir(serial).name
    jr_rows = db.read_rows(tag)
    curve_on = cv_volume.volume_cfg(cfg)["fines_curve_on"]
    fit = boil_fit_times(jr_rows) if curve_on else {}     # время «готовности» варок — для растяжения плавного порога
    fill = fill_seed_age(jr_rows) if curve_on else {}
    jr = {r["ts"]: r for r in jr_rows}
    for p in sorted(x for x in sd.iterdir() if x.is_dir()):
        if t_from is not None and (_ts_epoch(p.name) or 0) < t_from:
            continue
        try:
            # проверка по самому журналу: порог пробы в строке должен совпасть с тем, что даёт кривая для её времени с заводки и растяжения варки;
            # не совпал (проба «застряла» на средней кривой) — пересчитываем принудительно, не доверяя сравнению с её result.json
            j, force = jr.get(p.name), False
            if curve_on and j and j.get("fines_um") is not None and p.name in fit:
                sa = j.get("seed_age") if j.get("seed_age") is not None else fill.get(p.name)
                want = cv_volume.volume_cfg(cfg, j.get("phase"), sa, fit[p.name])["fines_um"] if sa is not None else None
                force = want is not None and abs(want - j["fines_um"]) > 0.05
            row = _hist_row(_with_volume(p, json.loads((p / "result.json").read_text(encoding="utf-8")), cfg, boil_t_s=fit.get(p.name),
                                         seed_age_s=fill.get(p.name), force=force))
            if row:
                fresh[row["ts"]] = row
        except Exception:
            continue
    n = 0
    tag = _serial_dir(serial).name
    cur = {r["ts"]: r for r in db.read_rows(tag)}
    for ts, row in fresh.items():
        if ts in cur and cur[ts] != row:
            db.upsert_row(tag, row)
            n += 1
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


def list_archive(serial: str) -> list[dict]:
    """Лента проб = ВСЕ пробы, что сейчас лежат в архиве кадров (новые сверху). Сколько их — решает настройка «Варок в архиве»
    (и потолок по месту): ротация удаляет старое, лента показывает то, что осталось. Лёгкие записи из журнала (SQLite) — без чтения
    result.json каждой пробы: ts, стадия, СВ, подстадия, число кристаллов, средний размер, разлом, кадров."""
    sd = _serial_dir(serial)
    if not sd.exists():
        return []
    dirs = sorted((p.name for p in sd.iterdir() if p.is_dir() and (p / "result.json").exists()), reverse=True)
    if not dirs:
        return []
    t0 = _ts_epoch(dirs[-1])
    rows = {r["ts"]: r for r in db.read_rows(sd.name, t_from=(t0 - 1) if t0 else None)}
    out = []
    for ts in dirs:
        r = rows.get(ts)
        if r is None:                         # пробы нет в журнале (ещё не залита) — минимальная запись
            out.append({"ts": ts, "stage": None, "sv": None, "substage": None, "summary": {}, "frames": 0, "fracture": {}})
            continue
        out.append({"ts": ts, "stage": r.get("stage"), "sv": r.get("sv"), "substage": r.get("substage"),
                    "summary": {"count": r.get("count"), "size_um": {"mean": r.get("mean")}},
                    "frames": r.get("frames") or 0,
                    "fracture": {"has_fracture": bool(r.get("frac_zones")), "zones": r.get("frac_zones")}})
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
