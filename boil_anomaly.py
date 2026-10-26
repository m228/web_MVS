"""boil_anomaly — нетипичные варки: законченная варка, которая выделяется из обычных по муке, размеру или сросткам.

Опора — до REF_N предыдущих типичных варок той же модели распознавания (с её порогом: метка модели), начатых ПОСЛЕ последней записи
«Журнала изменений» перед этой варкой: сменили рецепт / опыт — старые варки уже не «обычные». Опорных меньше MIN_REF — «мало данных»,
аномалий не объявляем. Граница — забор Тьюки (Q1 − K·IQR … Q3 + K·IQR), в обе стороны: слишком много и слишком мало муки — оба «не как обычно».
K = 3 (решение Макса 26.10: лучше пропустить слабую аномалию, чем тревожить зря): на Кире за 9–20.10 так помечается ~7 % варок, в основном
настоящие скачки уровня; K = 1,5 дал бы 17 %. Шум одной пробы (±22 %) в сводке варки уже усреднён по пробам финиша.
"""
from __future__ import annotations

from typing import Optional

import report

REF_N = 20                    # опорных варок (последние типичные той же модели)
MIN_REF = 8                   # меньше — «мало данных» (как в «одной варке»: 2 × MIN_N)
K = 3.0                       # ширина забора Тьюки, в IQR
LOOKBACK_S = 30 * 86400       # на сколько дней до начала периода искать опорные варки
KEYS = (("fines_avg", "мука на финише", "%", 1), ("fin_um", "размер на финише", "мкм", 0), ("fin_agg_pct", "сростки на финише", "%", 1),
        ("temp_avg", "температура", "°C", 1), ("vac_avg", "разрежение", "бар", 2))        # режим (ПЛК): среднее по варке
# режим меняется мало (Кир: разрежение −0,80 бар в 95 % проб, IQR = 0): ширина коридора — не меньше шага, который уже считается отклонением
FLOOR = {"temp_avg": 0.3, "vac_avg": 0.02, "temp": 0.5, "vac": 0.02}
# слова направления: разрежение хранится отрицательным (−0,80 бар), «выше» по числу = слабее по смыслу
WORDS = {"vac_avg": ("слабее", "глубже"), "vac": ("слабее", "глубже")}


def _word(key: str, d: str) -> str:
    w = WORDS.get(key) or ("выше", "ниже")
    return w[0] if d == "high" else w[1]


def _typical(r: dict) -> bool:
    return bool(r.get("finished")) and r.get("fin_um") is not None and not r.get("flag")


def assess(r: dict, ref: list) -> dict:
    """Варка r против опорных ref → {state: few | ok | anomaly, n, items: [{key, name, unit, nd, value, low, high, q1, q3, dir}]}."""
    out = {"state": "ok", "n": len(ref), "items": []}
    if len(ref) < MIN_REF:
        out["state"] = "few"
        return out
    for key, name, unit, nd in KEYS:
        v = r.get(key)
        rv = sorted(x[key] for x in ref if x.get(key) is not None)
        if v is None or len(rv) < MIN_REF:
            continue
        q1, q3 = report._quantile(rv, 0.25), report._quantile(rv, 0.75)
        w = max(q3 - q1, FLOOR.get(key, 0.0))
        lo, hi = q1 - K * w, q3 + K * w
        if key not in FLOOR:
            lo = max(0.0, lo)                                              # мука, размер, сростки не бывают отрицательными
        if v > hi or v < lo:
            d = "high" if v > hi else "low"
            out["items"].append({"key": key, "name": name, "unit": unit, "nd": nd, "value": v, "low": round(lo, 2), "high": round(hi, 2),
                                 "q1": q1, "q3": q3, "dir": d, "word": _word(key, d)})
    if out["items"]:
        out["state"] = "anomaly"
    return out


def mark(rows: list, pre: list, changes: list) -> None:
    """Пишет r['anom'] у варок периода rows (None — не оцениваем: идёт, короткая / затянутая, нет размера). pre — более ранние варки
    (опора), changes — журнал изменений серийника. rows и pre уже размечены flag."""
    allr = sorted(pre + rows, key=lambda x: x["t_start"])
    ch = sorted(c["t"] for c in changes)
    for r in rows:
        if not _typical(r):
            r["anom"] = None
            continue
        last = max((t for t in ch if t <= r["t_start"]), default=None)
        ref = [x for x in allr if _typical(x) and x["t_start"] < r["t_start"] and x.get("model") == r.get("model")
               and (last is None or x["t_start"] >= last)][-REF_N:]
        r["anom"] = assess(r, ref)
        r["anom"]["since_change"] = last is not None


def summary(rows: list) -> dict:
    """Сводка для отчёта: сколько варок оценено, сколько нетипичных и сколько без опоры."""
    a = [r["anom"] for r in rows if r.get("anom")]
    return {"k": K, "ref_n": REF_N, "min_ref": MIN_REF, "judged": sum(1 for x in a if x["state"] != "few"),
            "anomalies": sum(1 for x in a if x["state"] == "anomaly"), "few": sum(1 for x in a if x["state"] == "few")}


# --- этап (б): варка идёт — сравнение проб с коридором обычных варок по СВ ---
LIVE_N = 3                    # проб подряд за границей, в одну сторону (шум одной пробы ±22 % — одиночные выбросы не считаем)
SV_BIN = 0.5                  # шаг коридора по СВ; в коридор берём соседние бины тоже (СВ у пробы дрожит)
BIN_MIN = 30                  # меньше опорных проб в окне СВ — коридора нет, проба не оценивается
MIN_COUNT = 20                # кристаллов в пробе: меньше — проба пустая (сгущение, переход), не оцениваем
LIVE_KEYS = (("mean", "размер", "мкм", 0), ("fines", "мука", "%", 1), ("agg", "сростки", "%", 1), ("temp", "температура", "°C", 1), ("vac", "разрежение", "бар", 2))
_live_cache: dict = {}


def _feat(r: dict) -> Optional[dict]:
    """Показатели одной пробы для коридора: размер (мкм), мука по объёму (%), сростки по числу кристаллов (%). None — проба пустая."""
    import cv_store
    if r.get("sv") is None:
        return None
    f = {"temp": r.get("temp"), "vac": r.get("vac"), "mean": None, "fines": None, "agg": None}       # режим — у любой пробы, кристаллы — если их хватает
    if r.get("count") and r["count"] >= MIN_COUNT:
        f.update(mean=r.get("mean"), fines=cv_store.fines_avg(r), agg=100.0 * (r.get("n_aggregate") or 0) / r["count"])
    return f


def corridor(ref_groups: list) -> dict:
    """Опорные варки → {ключ: {бин СВ: [значения проб]}}: коридор «обычно» на каждом СВ."""
    out = {k: {} for k, *_ in LIVE_KEYS}
    for g in ref_groups:
        for r in g:
            f = _feat(r)
            if f is None:
                continue
            for k in out:
                if f[k] is not None:
                    out[k].setdefault(round(r["sv"] / SV_BIN), []).append(f[k])
    return out


def judge(probes: list, cor: dict) -> list:
    """Пробы текущей варки против коридора → [{key, dir, n, value, lo, hi, q1, q3, sv}] по показателям, где ПОСЛЕДНИЕ LIVE_N проб подряд
    вышли за границу (Q1 − K·IQR … Q3 + K·IQR при их СВ) в одну сторону. Проба без коридора / пустая обрывает серию."""
    out = []
    for key, name, unit, nd in LIVE_KEYS:
        run = []                                            # последняя серия проб за границей: (направление, значение, lo, hi, q1, q3, sv)
        for r in probes:
            f = _feat(r)
            v = f[key] if f else None
            b = round(r["sv"] / SV_BIN) if f else None
            ref = (cor[key].get(b - 1, []) + cor[key].get(b, []) + cor[key].get(b + 1, [])) if f else []
            if v is None or len(ref) < BIN_MIN:
                run = []
                continue
            ref.sort()
            q1, q3 = report._quantile(ref, 0.25), report._quantile(ref, 0.75)
            w = max(q3 - q1, FLOOR.get(key, 0.0))
            lo, hi = q1 - K * w, q3 + K * w
            if key not in FLOOR:
                lo = max(0.0, lo)
            d = "high" if v > hi else ("low" if v < lo else None)
            if d and run and run[-1][0] == d:
                run.append((d, v, lo, hi, q1, q3, r["sv"]))
            elif d:
                run = [(d, v, lo, hi, q1, q3, r["sv"])]
            else:
                run = []
        if len(run) >= LIVE_N:
            last = run[-LIVE_N:]
            vals = sorted(x[1] for x in last)
            out.append({"key": key, "name": name, "unit": unit, "nd": nd, "dir": last[-1][0], "word": _word(key, last[-1][0]), "n": len(run), "value": vals[len(vals) // 2],
                        "lo": round(last[-1][2], 2), "hi": round(last[-1][3], 2), "q1": last[-1][4], "q3": last[-1][5], "sv": last[-1][6]})
    return out


def live(serial: str, now: Optional[float] = None) -> dict:
    """Идущая варка против обычных: {state: idle | few | ok | anomaly, boil, n_ref, probes, items[]}. Результат кэшируется до новой пробы."""
    import time
    import cv_store
    now = time.time() if now is None else now
    groups = cv_store.boil_groups(serial)
    cur = groups[-1] if groups else None
    if not cur or now - cur[-1]["t"] > cv_store.BOIL_OPEN_S:
        return {"state": "idle"}
    key = (serial, cur[-1]["ts"], len(groups))
    hit = _live_cache.get(serial)
    if hit and hit[0] == key:
        return hit[1]
    t0 = cur[0]["t"]
    rep = report.build(serial, t0 - LOOKBACK_S, now, groups=groups)
    rows = {r["start"]: r for r in rep["boils"]}
    me = rows.get(cur[0]["ts"])
    out = {"state": "few", "boil": cur[0]["ts"], "n_ref": 0, "probes": len(cur), "items": [], "since_change": False, "k": K, "n": LIVE_N}
    if me is not None:
        ch = sorted(c["t"] for c in report.changes(serial, t0 - LOOKBACK_S, now))
        last = max((t for t in ch if t <= t0), default=None)
        by_ts = {g[0]["ts"]: g for g in groups}
        ref = [by_ts[x["start"]] for x in rep["boils"] if x["start"] != cur[0]["ts"] and _typical(x) and x["t_start"] < t0 and x.get("model") == me.get("model")
               and (last is None or x["t_start"] >= last) and x["start"] in by_ts][-REF_N:]
        out.update(n_ref=len(ref), since_change=last is not None)
        if len(ref) >= MIN_REF:
            out["items"] = judge(cur, corridor(ref))
            out["state"] = "anomaly" if out["items"] else "ok"
    _live_cache[serial] = (key, out)
    return out
