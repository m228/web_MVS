"""report — отчёт по варкам за период (неделя, месяц, свой): по каждой варке и итоги периода.

Данные — журнал проб (cv_store.boil_groups: варки с порогом «Мука с СВ», как в тренде). Варка входит в период по времени
начала. Мука — «Среднее» трёх моделей объёма (M1–M3), %; рассев — по ситам, % объёма (среднее M1–M3).

Состав:
  boils   — по каждой варке: начало/конец, длительность, проб, СВ мин/макс, мука НА ФИНИШЕ (пробы в пределах последних 1,5–2 СВ: среднее, 95% интервал, мин, макс), брак %,
            причины брака, разломы (зон / % площади, с учётом правок оператора), средний размер, рассев, подстадии,
            средние температура / разрежение по пробам стадий 3–9, часть суток по началу (part).
  totals  — итоги: число варок, медиана / «обычно» (25–75 %) / среднее / мин / макс по основным показателям, лучшая/худшая варка (по муке).
            Показатели — только по типичным варкам: законченная, не короткая и не затянутая (flag). Число варок и проб — по всем.
  days    — те же итоги по дням (+ вода, средний рассев); weeks — по неделям.
  segments — отрезки периода по модели распознавания: разные модели по-разному видят мелочь, их цифры не сравнивают.
  day_parts — те же итоги по частям суток (утро / день / вечер / ночь — половины смен 08–20 и 20–08): день и ночь на неделе.
  substages — по группам подстадий (подкачка / рост 1 / рост 2 / прочие): проб, время, средняя мука.
"""
from __future__ import annotations

import csv
import io
import time
from typing import Optional

import cv_store
import cv_volume
import db
import substages

SIEVE_LABELS = ("дно <0,2", "0,2–0,5", "0,5–0,7", "0,7–0,8", "0,8–1", ">1")      # >1 = 1–1,2 + больше 1,2 (у маточника крупных нет)
_GROUP_NAMES = {"pump": "подкачка", "g1": "рост 1", "g2": "рост 2", None: "прочие / неизвестна"}


def _mean(vals: list) -> Optional[float]:
    v = [x for x in vals if x is not None]
    return round(sum(v) / len(v), 3) if v else None


def _avg3(o: dict) -> Optional[float]:
    v = [o.get(k) for k in ("m1", "m2", "m3") if o.get(k) is not None]
    return round(sum(v) / len(v), 3) if v else None


def _sieve_rows(sv: dict) -> list:
    """Рассев по 6 строкам (дно … «>1»): среднее M1–M3, %; последние две фракции склеены."""
    def at(i):
        return _avg3({m: ((sv.get(m) or [None] * 7)[i]) for m in ("m1", "m2", "m3")})
    vals = [at(i) for i in range(5)]
    tail = [x for x in (at(5), at(6)) if x is not None]
    vals.append(round(sum(tail), 3) if tail else None)
    return vals


FIN_SV = 2.0        # «финиш» варки: пробы, у которых СВ не ниже конечного СВ минус столько единиц


_finish_probes = cv_store.finish_probes      # одно правило «финиша» для отчёта и окна «Объём и мука»
PROC_STAGES = (3, 9)    # стадии варки, где идут авто-пробы (microscope_fsm.CYCLE_STAGE_MIN/MAX): ручные пробы на останове и пропарке (99 °C, вакуума нет) в режим варки не идут


def _boil_row(g: list, summary: dict, fin_sv: float = FIN_SV) -> dict:
    ok_sv = [r["sv"] for r in g if r.get("sv") is not None and r["sv"] >= 5]
    proc = [r for r in g if r.get("stage") is None or PROC_STAGES[0] <= r["stage"] <= PROC_STAGES[1]]
    fin = [r for r in g if r.get("fines_m3") is not None]
    # мука варки = ФИНИШ: все пробы, где СВ в пределах fin_sv от конечного (последние 1,5–2 СВ). Среднее по ВСЕМ пробам после порога
    # «Мука с СВ» завышено: при низком пороге туда попадает начало варки, где кристаллы ещё мелкие (мука до 100 %)
    sel, sv_ref = _finish_probes(fin, fin_sv)
    block = cv_store._vol_block(sel)                      # среднее мука/рассев по пробам финиша, с весом по объёму пробы
    fa = [cv_store.fines_avg(r) for r in sel]
    fa = [x for x in fa if x is not None]
    dur = (g[-1]["t"] - g[0]["t"]) / 60.0
    all_b = summary.get("all") or {}
    return {
        "start": g[0]["ts"], "end": g[-1]["ts"], "t_start": g[0]["t"], "t_end": g[-1]["t"],
        "duration_min": round(dur, 1), "probes": len(g), "finish_probes": len(sel),
        "sv_min": min(ok_sv) if ok_sv else None, "sv_max": max(ok_sv) if ok_sv else None, "sv_end": sv_ref,
        "fines_avg": _avg3(block.get("fines") or {}), "fines_min": min(fa) if fa else None, "fines_max": max(fa) if fa else None,
        "fines_lo": _avg3(block.get("fines_lo") or {}), "fines_hi": _avg3(block.get("fines_hi") or {}),     # 95% интервал муки финиша (t-оценка по пробам)
        "fines_all": _avg3(all_b.get("fines") or {}),
        "reject_pct": _mean([r.get("reject_pct") for r in g]),
        "needle": _mean([r.get("n_needle") for r in g]), "aggregate": _mean([r.get("n_aggregate") for r in g]),
        "crooked": _mean([r.get("n_crooked") for r in g]),
        "frac_zones": sum(int(r.get("frac_zones") or 0) for r in g), "frac_pct": _mean([r.get("frac_pct") for r in g]),
        "mean_um": _mean([r.get("mean") for r in g]), "cv_pct": _mean([r.get("cv_pct") for r in g]),
        "sieve": _sieve_rows(block.get("sieve") or {}),
        # режим варки (ПЛК на момент проб; другой истории ПЛК нет): средняя температура в аппарате, °C, и разрежение сверху, бар
        "temp_avg": _mean([r.get("temp") for r in proc]), "vac_avg": _mean([r.get("vac") for r in proc]),
    }


def _substage_rows(groups: list[list[dict]]) -> list[dict]:
    """По группам подстадий: проб, время (по интервалам между пробами), средняя мука среди проб с мукой."""
    acc = {}
    for g in groups:
        for i, r in enumerate(g):
            grp = substages.group_of(r.get("substage")) if r.get("substage") is not None else None
            a = acc.setdefault(grp, {"probes": 0, "min": 0.0, "fines": []})
            a["probes"] += 1
            nxt = g[i + 1]["t"] if i + 1 < len(g) else None
            if nxt is not None and nxt - r["t"] <= 600:
                a["min"] += (nxt - r["t"]) / 60.0
            f = cv_store.fines_avg(r)
            if f is not None:
                a["fines"].append(f)
    order = ["pump", "g1", "g2", None]
    return [{"group": _GROUP_NAMES[k], "probes": acc[k]["probes"], "minutes": round(acc[k]["min"], 1),
             "fines_avg": _mean(acc[k]["fines"]), "fines_min": min(acc[k]["fines"]) if acc[k]["fines"] else None,
             "fines_max": max(acc[k]["fines"]) if acc[k]["fines"] else None} for k in order if k in acc]


SHORT_K = 0.6      # варка короче медианы периода во столько раз — короткая (оборвали, остановка): в таблице с пометкой, в итоги не идёт
LONG_K = 1.5       # длиннее — затянутая (простой, две варки слились); то же правило, что в сравнении групп (boil_compare)


def _quantile(v: list, p: float) -> Optional[float]:
    """Квантиль с линейной интерполяцией (как Excel «ПРОЦЕНТИЛЬ.ВКЛ»); v отсортирован."""
    if not v:
        return None
    k = (len(v) - 1) * p
    i = int(k)
    return round(v[i] + (v[min(i + 1, len(v) - 1)] - v[i]) * (k - i), 3)


def _mark_flags(rows: list[dict]) -> None:
    """flag варки: «open» — идёт, «short» / «long» — короче / длиннее медианы законченных варок периода в SHORT_K / LONG_K раз.
    Меньше 3 законченных варок — медиана ничего не значит, короткие/затянутые не ищем."""
    done = sorted(r["duration_min"] for r in rows if r.get("finished", True))
    med = _quantile(done, 0.5) if len(done) >= 3 else None
    for r in rows:
        if not r.get("finished", True):
            r["flag"] = "open"
        elif med and r["duration_min"] < SHORT_K * med:
            r["flag"] = "short"
        elif med and r["duration_min"] > LONG_K * med:
            r["flag"] = "long"
        else:
            r["flag"] = None


def _totals(rows: list[dict]) -> dict:
    typ = [r for r in rows if not r.get("flag")]          # показатели — по типичным варкам; без флагов (старые вызовы) — по всем

    def mm(k):
        v = sorted(r[k] for r in typ if r.get(k) is not None)
        return {"avg": _mean(v), "min": v[0] if v else None, "max": v[-1] if v else None, "n": len(v),
                "median": _quantile(v, 0.5), "q1": _quantile(v, 0.25), "q3": _quantile(v, 0.75)}
    withf = [r for r in typ if r.get("fines_avg") is not None]
    best = min(withf, key=lambda r: r["fines_avg"]) if withf else None
    worst = max(withf, key=lambda r: r["fines_avg"]) if withf else None
    sv = [r["sieve"] for r in typ if r.get("sieve") and any(x is not None for x in r["sieve"])]
    return {"boils": len(rows), "probes": sum(r["probes"] for r in rows), "typical": len(typ),
            "short": sum(1 for r in rows if r.get("flag") == "short"), "long": sum(1 for r in rows if r.get("flag") == "long"),
            "duration_min": mm("duration_min"), "fines_avg": mm("fines_avg"), "reject_pct": mm("reject_pct"),
            "frac_zones": sum(r["frac_zones"] for r in rows), "frac_boils": sum(1 for r in rows if r["frac_zones"]),
            "mean_um": mm("mean_um"), "sv_max": mm("sv_max"),
            "point_um": mm("point_um"), "fin_um": mm("fin_um"), "grow_um": mm("grow_um"), "to_ready_min": mm("to_ready_min"),
            "fin_cv": mm("fin_cv"), "fin_agg_pct": mm("fin_agg_pct"), "seed_ready_min": mm("seed_ready_min"), "seed_end_min": mm("seed_end_min"),
            "temp_avg": mm("temp_avg"), "vac_avg": mm("vac_avg"),
            "water": sum(1 for r in typ if r.get("water") is True), "water_known": sum(1 for r in typ if r.get("water") is not None),
            "sieve": [_mean([s[i] for s in sv]) for i in range(len(SIEVE_LABELS))] if sv else None,     # средний рассев (каждая варка с равным весом)
            "best": {"start": best["start"], "fines_avg": best["fines_avg"]} if best else None,
            "worst": {"start": worst["start"], "fines_avg": worst["fines_avg"]} if worst else None}


def _segments(rows: list[dict]) -> list[dict]:
    """Подряд идущие варки одной модели распознавания (None — модель не записана, пробы до 1.9.76) с итогами по каждому отрезку.
    Варки без записи между двумя отрезками одной модели (A, None, A — например, пробы без паспорта модели) — та же модель, не смена."""
    runs = []
    for r in rows:
        if runs and runs[-1][0] == r.get("model"):
            runs[-1][1].append(r)
        else:
            runs.append((r.get("model"), [r]))
    i = 1
    while i + 1 < len(runs):
        if runs[i][0] is None and runs[i - 1][0] is not None and runs[i - 1][0] == runs[i + 1][0]:
            runs[i - 1:i + 2] = [(runs[i - 1][0], runs[i - 1][1] + runs[i][1] + runs[i + 1][1])]
        else:
            i += 1
    return [{"model": m, "from": g[0]["start"], "to": g[-1]["start"], **_totals(g)} for m, g in runs]


MIN_PROBES = 5      # варки короче (обрывки, пробы без варки) в отчёт не берём

# части суток (фон графиков и табличка на экране): смены 08–20 и 20–08, каждая пополам; одинаково на Буи и Кире. (ключ, имя, с часа, до часа)
DAY_PARTS = (("morning", "утро", 8, 14), ("day", "день", 14, 20), ("evening", "вечер", 20, 2), ("night", "ночь", 2, 8))


def _day_part(t: float) -> str:
    """Часть суток по местному часу момента t (варка — по началу: заводка решает больше всего)."""
    h = time.localtime(t).tm_hour
    return next(k for k, _, a, b in DAY_PARTS if (a <= h < b if a < b else h >= a or h < b))


def _point_sv() -> float:
    """Контрольная точка СВ завода (cv.point_sv); конфига нет — 86."""
    try:
        import plate_config
        return float((plate_config.load().get("cv") or {}).get("point_sv") or cv_store.POINT_SV)
    except Exception:
        return cv_store.POINT_SV


SEED_TARGET = {"fines_max": 10.0, "size_min_um": 200.0, "size_max_um": 300.0}


def seed_target() -> dict:
    """Цели маточника (cv.seed_target): мука на финише не выше fines_max, %; размер на финише в коридоре size_min_um…size_max_um, мкм."""
    try:
        import plate_config
        cfg = (plate_config.load().get("cv") or {}).get("seed_target") or {}
    except Exception:
        cfg = {}
    return {k: float(cfg[k]) if cfg.get(k) is not None else v for k, v in SEED_TARGET.items()}


def changes(serial: str, t_from: Optional[float] = None, t_to: Optional[float] = None) -> list[dict]:
    """Журнал изменений процесса (db.changes) по серийнику, с подписью времени «дд.мм чч:мм»."""
    out = db.changes(cv_store._serial_dir(serial).name, t_from, t_to)
    for c in out:
        c["label"] = time.strftime("%d.%m %H:%M", time.localtime(c["t"]))
    return out


def _model_drift(t_from: float, t_to: float) -> Optional[dict]:
    """Предупреждение «проверка модели давала разный результат» за период (model_check.drift); нет базы / проверок — None."""
    try:
        import model_check
        return model_check.drift(t_from, t_to)
    except Exception:
        return None


def build(serial: str, t_from: float, t_to: float, min_probes: int = MIN_PROBES, fin_sv: float = FIN_SV, groups: Optional[list] = None,
          anomalies: bool = False) -> dict:
    """Отчёт за период [t_from, t_to] (epoch, сек). Варка входит по времени начала; обрывки короче min_probes проб пропускаются.
    fin_sv — «финиш» для муки/рассева: пробы в пределах стольких СВ от конечного. groups — уже прочитанные cv_store.boil_groups (сравнение
    берёт по ним ещё и кривые роста — журнал читается один раз). anomalies — пометить нетипичные варки (boil_anomaly): нужен запас прошлых варок
    для опоры, поэтому журнал за месяц до периода тоже разбирается."""
    groups = cv_store.boil_groups(serial) if groups is None else groups
    avg_n = cv_volume.volume_cfg(cv_store._volume_cfg_now())["avg_n"]
    now = time.time()
    sel = [g for g in groups if t_from <= g[0]["t"] <= t_to and len(g) >= min_probes]
    point_sv = _point_sv()

    def _rows(gs: list) -> list:
        out = []
        for g in gs:
            finished = not (g is groups[-1] and now - g[-1]["t"] <= cv_store.BOIL_OPEN_S)
            row = _boil_row(g, cv_store._boil_summary(g, finished, avg_n), fin_sv)
            row.update(cv_store.boil_metrics(g, point_sv, fin_sv))
            row["finished"] = finished
            row["part"] = _day_part(row["t_start"])
            out.append(row)
        return out
    rows = _rows(sel)
    _mark_flags(rows)
    anom = None
    if anomalies:
        import boil_anomaly
        pre = _rows([g for g in groups if t_from - boil_anomaly.LOOKBACK_S <= g[0]["t"] < t_from and len(g) >= min_probes])
        _mark_flags(pre)                       # своя медиана длительности у прошлых варок: опорными идут только типичные
        boil_anomaly.mark(rows, pre, changes(serial, t_from - boil_anomaly.LOOKBACK_S, t_to))
        anom = boil_anomaly.summary(rows)
    weeks, days = {}, {}
    for r in rows:
        y, w, _ = time.localtime(r["t_start"]).tm_year, time.strftime("%W", time.localtime(r["t_start"])), 0
        weeks.setdefault("%s-W%s" % (y, w), []).append(r)
        days.setdefault(time.strftime("%Y-%m-%d", time.localtime(r["t_start"])), []).append(r)
    return {
        "serial": serial, "from": t_from, "to": t_to, "generated": now, "min_probes": min_probes, "fin_sv": fin_sv, "point_sv": point_sv, "fines_from_sv": cv_volume.volume_cfg(cv_store._volume_cfg_now())["fines_from_sv"],
        "sieve_labels": list(SIEVE_LABELS), "short_k": SHORT_K, "long_k": LONG_K,
        "boils": rows, "totals": _totals(rows),
        "weeks": [{"week": k, **_totals(v)} for k, v in sorted(weeks.items())],
        "days": [{"day": k, **_totals(v)} for k, v in sorted(days.items())],
        "segments": _segments(rows),
        "day_parts": [{"key": k, "name": n, "from": a, "to": b, **_totals([r for r in rows if r["part"] == k])} for k, n, a, b in DAY_PARTS],
        "substages": _substage_rows(sel),
        "targets": seed_target(), "changes": changes(serial, t_from, t_to), "anomalies": anom, "model_check": _model_drift(t_from, t_to),
    }


_BOIL_COLS = [("start", "начало"), ("end", "конец"), ("duration_min", "длительность, мин"), ("probes", "проб"), ("sv_min", "СВ мин"), ("sv_max", "СВ макс"),
              ("sv_end", "конечное СВ"), ("finish_probes", "проб финиша"), ("fines_avg", "мука на финише, % (последние СВ)"),
              ("fines_lo", "мука финиша, 95% от"), ("fines_hi", "мука финиша, 95% до"), ("fines_min", "мука мин, % (финиш)"), ("fines_max", "мука макс, % (финиш)"),
              ("fines_all", "мука, % (все пробы после «Мука с СВ»)"), ("reject_pct", "брак, %"),
              ("needle", "иглы, шт/кадр"), ("aggregate", "сростки, шт/кадр"), ("crooked", "кривые, шт/кадр"), ("frac_zones", "разломов, зон"),
              ("frac_pct", "разлом, % площади"), ("mean_um", "средний размер, мкм"),
              ("water", "вода на стабилизации"), ("water_dose", "доза воды, %·с"), ("point_um", "размер на контрольной точке СВ, мкм"),
              ("fin_um", "размер на финише, мкм"), ("grow_um", "прирост от точки до финиша, мкм"), ("to_ready_min", "от точки до готовности, мин"),
              ("seed_ready_min", "заводка → готовность, мин"), ("seed_end_min", "заводка → последняя проба, мин"),
              ("fin_cv", "однородность: разброс размера на финише, %"), ("fin_agg_pct", "сростки на финише, % по числу"),
              ("model", "модель распознавания"), ("flag", "пометка (в итоги не идёт)")]
_FLAG_NAMES = {"open": "идёт", "short": "короткая", "long": "затянутая"}


def _cell(r: dict, k: str):
    v = r.get(k)
    if k == "water" and v is not None:
        return ("да" if v else "нет") + (" (по СВ)" if r.get("water_by") == "sv" else "")
    if k == "flag":
        return _FLAG_NAMES.get(v, "")
    return "" if v is None else v


def to_csv(rep: dict, cols: Optional[list] = None) -> bytes:
    """CSV отчёта (UTF-8 с BOM, разделитель «;» — открывается в Excel): таблица варок, итоги, подстадии."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Отчёт по варкам", rep["serial"], time.strftime("%d.%m.%Y", time.localtime(rep["from"])) + " — " + time.strftime("%d.%m.%Y", time.localtime(rep["to"]))])
    w.writerow([])
    legacy = ("point_um", "grow_um", "to_ready_min")         # контрольная точка СВ 86 (опыт Кира с водой): с экрана убрана, в CSV — если есть данные
    old = any(r.get("point_um") is not None for r in rep["boils"])
    bc = [(k, h) for k, h in _BOIL_COLS if k in ("start", "end", "flag") or cols is None or k in cols or (old and k in legacy)]     # cols — выбранные в интерфейсе
    with_sieve = cols is None or "sieve" in cols
    w.writerow([h for _, h in bc] + (["рассев " + s + ", %" for s in rep["sieve_labels"]] if with_sieve else []))
    for r in rep["boils"]:
        w.writerow([_cell(r, k) for k, _ in bc] + (["" if v is None else v for v in r["sieve"]] if with_sieve else []))
    w.writerow([])
    t = rep["totals"]
    want = (lambda k: cols is None or k in cols)                 # итоги — только по выбранным столбцам
    w.writerow(["Итого: варок", t["boils"], "проб", t["probes"], "в итогах (без идущей, коротких и затянутых)", t["typical"]])
    stat = (("median", "медиана"), ("q1", "25 %"), ("q3", "75 %"), ("avg", "среднее"), ("min", "мин"), ("max", "макс"))
    for key, name, ck in (("duration_min", "длительность, мин", "duration_min"), ("fines_avg", "мука на финише, %", "fines_avg"),
                          ("reject_pct", "брак, %", "reject_pct"), ("mean_um", "размер, мкм", "mean_um"), ("sv_max", "СВ макс", "sv_max"),
                          ("point_um", "размер на контрольной точке СВ, мкм", "point_um"), ("fin_um", "размер на финише, мкм", "fin_um"),
                          ("grow_um", "прирост от точки до финиша, мкм", "grow_um"),
                          ("fin_cv", "однородность: разброс размера на финише, %", "fin_cv"), ("fin_agg_pct", "сростки на финише, %", "fin_agg_pct"),
                          ("seed_ready_min", "заводка → готовность, мин", "seed_ready_min"), ("seed_end_min", "заводка → последняя проба, мин", "seed_end_min")):
        if want(ck):
            m = t[key]
            w.writerow([name] + [x for k, h in stat for x in (h, "" if m[k] is None else m[k])])
    if want("frac_zones"):
        w.writerow(["Разломов, зон", t["frac_zones"], "варок с разломом", t["frac_boils"]])
    if t["best"] and want("fines_avg"):
        w.writerow(["Лучшая варка (меньше муки)", t["best"]["start"], t["best"]["fines_avg"]])
        w.writerow(["Худшая варка (больше муки)", t["worst"]["start"], t["worst"]["fines_avg"]])
    if want("fines_avg"):
        w.writerow([])
        w.writerow(["Подстадии", "проб", "минут", "мука среднее, %", "мука мин, %", "мука макс, %"])
        for s in rep["substages"]:
            w.writerow([s["group"], s["probes"], s["minutes"]] + ["" if s[k] is None else s[k] for k in ("fines_avg", "fines_min", "fines_max")])
    w.writerow([])
    head = ["Недели", "варок"] + (["мука на финише, % среднее"] if want("fines_avg") else []) + (["брак среднее, %"] if want("reject_pct") else [])
    w.writerow(head)
    for k in rep["weeks"]:
        w.writerow([k["week"], k["boils"]] + (["" if k["fines_avg"]["avg"] is None else k["fines_avg"]["avg"]] if want("fines_avg") else []) +
                   (["" if k["reject_pct"]["avg"] is None else k["reject_pct"]["avg"]] if want("reject_pct") else []))
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")
