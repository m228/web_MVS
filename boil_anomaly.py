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
KEYS = (("fines_avg", "мука на финише", "%", 1), ("fin_um", "размер на финише", "мкм", 0), ("fin_agg_pct", "сростки на финише", "%", 1))


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
        lo, hi = max(0.0, q1 - K * (q3 - q1)), q3 + K * (q3 - q1)         # мука, размер, сростки не бывают отрицательными
        if v > hi or v < lo:
            out["items"].append({"key": key, "name": name, "unit": unit, "nd": nd, "value": v, "low": round(lo, 2), "high": round(hi, 2),
                                 "q1": q1, "q3": q3, "dir": "high" if v > hi else "low"})
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
