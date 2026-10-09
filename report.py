"""report — отчёт по варкам за период (неделя, месяц, свой): по каждой варке и итоги периода.

Данные — журнал проб (cv_store.boil_groups: варки с порогом «Мука с СВ», как в тренде). Варка входит в период по времени
начала. Мука — «Среднее» трёх моделей объёма (M1–M3), %; рассев — по ситам, % объёма (среднее M1–M3).

Состав:
  boils   — по каждой варке: начало/конец, длительность, проб, СВ мин/макс, мука НА ФИНИШЕ (пробы в пределах последних 1,5–2 СВ: среднее, мин, макс), брак %,
            причины брака, разломы (зон / % площади, с учётом правок оператора), средний размер, рассев, подстадии.
  totals  — итоги: число варок, среднее/мин/макс по основным показателям, лучшая/худшая варка (по муке).
  weeks   — те же итоги по неделям внутри периода.
  substages — по группам подстадий (подкачка / рост 1 / рост 2 / прочие): проб, время, средняя мука.
"""
from __future__ import annotations

import csv
import io
import time
from typing import Optional

import cv_store
import cv_volume
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


def _finish_probes(fin: list, fin_sv: float) -> tuple[list, Optional[float]]:
    """Пробы финиша: СВ в пределах fin_sv (последние 1,5–2 СВ) от конечного. Конечное СВ — медиана СВ последних 5 проб с мукой
    (одиночный выброс или провал СВ на подкачке не сдвигает отсчёт). Нет СВ — последние 4 пробы."""
    svs = [r["sv"] for r in fin[-5:] if r.get("sv") is not None and r["sv"] >= 5]
    if not svs:
        return fin[-4:], None
    ref = sorted(svs)[len(svs) // 2]
    sel = [r for r in fin if r.get("sv") is not None and r["sv"] >= ref - fin_sv]
    return (sel or fin[-4:]), ref


def _boil_row(g: list, summary: dict, fin_sv: float = FIN_SV) -> dict:
    ok_sv = [r["sv"] for r in g if r.get("sv") is not None and r["sv"] >= 5]
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
        "fines_all": _avg3(all_b.get("fines") or {}),
        "reject_pct": _mean([r.get("reject_pct") for r in g]),
        "needle": _mean([r.get("n_needle") for r in g]), "aggregate": _mean([r.get("n_aggregate") for r in g]),
        "crooked": _mean([r.get("n_crooked") for r in g]),
        "frac_zones": sum(int(r.get("frac_zones") or 0) for r in g), "frac_pct": _mean([r.get("frac_pct") for r in g]),
        "mean_um": _mean([r.get("mean") for r in g]), "cv_pct": _mean([r.get("cv_pct") for r in g]),
        "sieve": _sieve_rows(block.get("sieve") or {}),
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


def _totals(rows: list[dict]) -> dict:
    def col(k):
        return [r[k] for r in rows if r.get(k) is not None]

    def mm(k):
        v = col(k)
        return {"avg": _mean(v), "min": min(v) if v else None, "max": max(v) if v else None}
    withf = [r for r in rows if r.get("fines_avg") is not None]
    best = min(withf, key=lambda r: r["fines_avg"]) if withf else None
    worst = max(withf, key=lambda r: r["fines_avg"]) if withf else None
    return {"boils": len(rows), "probes": sum(r["probes"] for r in rows),
            "duration_min": mm("duration_min"), "fines_avg": mm("fines_avg"), "reject_pct": mm("reject_pct"),
            "frac_zones": sum(r["frac_zones"] for r in rows), "frac_boils": sum(1 for r in rows if r["frac_zones"]),
            "mean_um": mm("mean_um"), "sv_max": mm("sv_max"),
            "best": {"start": best["start"], "fines_avg": best["fines_avg"]} if best else None,
            "worst": {"start": worst["start"], "fines_avg": worst["fines_avg"]} if worst else None}


MIN_PROBES = 5      # варки короче (обрывки, пробы без варки) в отчёт не берём


def build(serial: str, t_from: float, t_to: float, min_probes: int = MIN_PROBES, fin_sv: float = FIN_SV) -> dict:
    """Отчёт за период [t_from, t_to] (epoch, сек). Варка входит по времени начала; обрывки короче min_probes проб пропускаются.
    fin_sv — «финиш» для муки/рассева: пробы в пределах стольких СВ от конечного."""
    groups = cv_store.boil_groups(serial)
    avg_n = cv_volume.volume_cfg(cv_store._volume_cfg_now())["avg_n"]
    now = time.time()
    sel = [g for g in groups if t_from <= g[0]["t"] <= t_to and len(g) >= min_probes]
    rows = []
    for g in sel:
        finished = not (g is groups[-1] and now - g[-1]["t"] <= cv_store.BOIL_OPEN_S)
        row = _boil_row(g, cv_store._boil_summary(g, finished, avg_n), fin_sv)
        row["finished"] = finished
        rows.append(row)
    weeks = {}
    for r in rows:
        y, w, _ = time.localtime(r["t_start"]).tm_year, time.strftime("%W", time.localtime(r["t_start"])), 0
        weeks.setdefault("%s-W%s" % (y, w), []).append(r)
    return {
        "serial": serial, "from": t_from, "to": t_to, "generated": now, "min_probes": min_probes, "fin_sv": fin_sv, "fines_from_sv": cv_volume.volume_cfg(cv_store._volume_cfg_now())["fines_from_sv"],
        "sieve_labels": list(SIEVE_LABELS),
        "boils": rows, "totals": _totals(rows),
        "weeks": [{"week": k, **_totals(v)} for k, v in sorted(weeks.items())],
        "substages": _substage_rows(sel),
    }


_BOIL_COLS = [("start", "начало"), ("end", "конец"), ("duration_min", "длительность, мин"), ("probes", "проб"), ("sv_min", "СВ мин"), ("sv_max", "СВ макс"),
              ("sv_end", "конечное СВ"), ("finish_probes", "проб финиша"), ("fines_avg", "мука на финише, % (последние СВ)"), ("fines_min", "мука мин, % (финиш)"), ("fines_max", "мука макс, % (финиш)"),
              ("fines_all", "мука, % (все пробы после «Мука с СВ»)"), ("reject_pct", "брак, %"),
              ("needle", "иглы, шт/кадр"), ("aggregate", "сростки, шт/кадр"), ("crooked", "кривые, шт/кадр"), ("frac_zones", "разломов, зон"),
              ("frac_pct", "разлом, % площади"), ("mean_um", "средний размер, мкм")]


def to_csv(rep: dict, cols: Optional[list] = None) -> bytes:
    """CSV отчёта (UTF-8 с BOM, разделитель «;» — открывается в Excel): таблица варок, итоги, подстадии."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Отчёт по варкам", rep["serial"], time.strftime("%d.%m.%Y", time.localtime(rep["from"])) + " — " + time.strftime("%d.%m.%Y", time.localtime(rep["to"]))])
    w.writerow([])
    bc = [(k, h) for k, h in _BOIL_COLS if k in ("start", "end") or cols is None or k in cols]       # cols — выбранные в интерфейсе столбцы
    with_sieve = cols is None or "fines_avg" in cols                                                   # рассев и мука идут вместе
    w.writerow([h for _, h in bc] + (["рассев " + s + ", %" for s in rep["sieve_labels"]] if with_sieve else []))
    for r in rep["boils"]:
        w.writerow([("" if r.get(k) is None else r.get(k)) for k, _ in bc] + (["" if v is None else v for v in r["sieve"]] if with_sieve else []))
    w.writerow([])
    t = rep["totals"]
    want = (lambda k: cols is None or k in cols)                 # итоги — только по выбранным столбцам
    w.writerow(["Итого: варок", t["boils"], "проб", t["probes"]])
    for key, name, ck in (("duration_min", "длительность, мин", "duration_min"), ("fines_avg", "мука на финише, %", "fines_avg"),
                          ("reject_pct", "брак, %", "reject_pct"), ("mean_um", "размер, мкм", "mean_um"), ("sv_max", "СВ макс", "sv")):
        if want(ck):
            m = t[key]
            w.writerow([name, "среднее", "" if m["avg"] is None else m["avg"], "мин", "" if m["min"] is None else m["min"], "макс", "" if m["max"] is None else m["max"]])
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
