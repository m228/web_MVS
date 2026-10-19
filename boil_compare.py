"""boil_compare — сравнение двух групп варок (опыт против контроля): группа = период (с точностью до минуты) и/или вода да/нет.

В сравнение идёт законченная варка, у которой есть размер на контрольной точке СВ, финиш и готовность (report.build → cv_store.boil_metrics);
затянутые (длиннее LONG_K медиан: простой, слившиеся варки) не идут — их список в ответе («excluded»).
По каждому показателю: число варок, медиана, среднее, разброс (σ), разница средних A − B и p перестановочного теста (при небольших группах —
полный перебор, иначе случайные перестановки с фиксированным зерном: ответ не прыгает от запроса к запросу). Тест по средним, не по медианам:
при 4–6 варках медиана почти не двигается между разбиениями и тест слепнет (полностью разделённые группы 6 против 10 давали p 0,12).
Итог варки предопределён к СВ точки (Кир, 1280: размер на 86 ↔ размер финиша r 0,94, ↔ мука финиша r −0,96), поэтому для итога ещё и разница
ПРИ РАВНОМ размере на точке: линейная модель y ~ группа + размер на точке, 95 % интервал. Без поправки разница финиша — это разница стартов.
Вердикт: «разница реальная» / «мало варок — нужно ~N» / «в пределах шума». Разные модели распознавания в группах — сравнение запрещено:
модель меняет абсолютные цифры размера и муки (Кир 16.10 19:35, 640 → 1280).
"""
from __future__ import annotations

import itertools
import math
import random
import statistics as st
import time
from typing import Optional

import cv_store
import report

MIN_N = 4           # меньше варок в группе — перестановочный тест не может дать p < 0,05 (3 против 16: минимум p ≈ 0,07)
P_REAL = 0.05
PERM_N = 20000      # перестановок, если полный перебор длиннее
NEED_MAX = 40       # нужно больше варок на группу (≈ 4 дня варок Кира) — разница мала, «в пределах шума»
Z_POWER = 2.8       # 1,96 + 0,84: 95 % уверенность, 80 % шанс поймать разницу такого размера
LONG_K = 1.5        # варка длиннее медианы обеих групп во столько раз — затянута (простой, две варки слились), в сравнение не идёт

# (ключ, название, поправка на размер на точке)
METRICS = [("point_um", "размер на контрольной точке СВ, мкм", False),
           ("fin_um", "размер на финише, мкм", False),
           ("grow_um", "прирост от точки до финиша, мкм", True),
           ("grow_pct", "прирост от точки до финиша, %", True),
           ("fines_avg", "мука на финише, %", True),
           ("to_ready_min", "от точки до готовности, мин", False)]


def parse_t(v, end: bool = False) -> Optional[float]:
    """Время границы группы: epoch, «YYYY-MM-DD» (конец — до 23:59:59) или «YYYY-MM-DD HH:MM» / «…THH:MM». Пусто — None."""
    if v in (None, ""):
        return None
    try:
        return float(v)
    except ValueError:
        pass
    s = str(v).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except ValueError:
            continue
    return time.mktime(time.strptime(s, "%Y-%m-%d")) + (86399 if end else 0)


def _eligible(b: dict) -> bool:
    return bool(b.get("finished")) and all(b.get(k) is not None for k in ("point_um", "fin_um", "to_ready_min"))


def _in_group(b: dict, g: dict) -> bool:
    return g["from"] <= b["t_start"] <= g["to"] and (g.get("water") is None or b.get("water") is g["water"])


def _stats(v: list) -> dict:
    return {"n": len(v), "median": round(st.median(v), 2) if v else None, "mean": round(st.fmean(v), 2) if v else None, "sd": round(st.stdev(v), 2) if len(v) >= 2 else None,
            "min": min(v) if v else None, "max": max(v) if v else None}


def perm_p(a: list, b: list, n: int = PERM_N, seed: int = 1) -> Optional[float]:
    """p перестановочного теста для разницы средних (двусторонний): доля разбиений общего набора, где разница не меньше наблюдаемой."""
    if not a or not b:
        return None
    pool, k = a + b, len(a)
    tot_sum, m = sum(pool), len(pool) - len(a)

    def gap(s_a):                                               # |среднее A − среднее B| по сумме A
        return abs(s_a / k - (tot_sum - s_a) / m)
    obs = gap(sum(a)) - 1e-9
    if math.comb(len(pool), k) <= n:                            # полный перебор
        hit = tot = 0
        for comb in itertools.combinations(pool, k):
            hit += gap(sum(comb)) >= obs
            tot += 1
        return hit / tot
    rnd, hit = random.Random(seed), 0
    for _ in range(n):
        rnd.shuffle(pool)
        hit += gap(sum(pool[:k])) >= obs
    return (hit + 1) / (n + 1)


def _solve3(m: list, v: list) -> Optional[list]:
    """Решение 3×3 (метод Гаусса); вырождена — None."""
    a = [row[:] + [v[i]] for i, row in enumerate(m)]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) < 1e-12:
            return None
        a[c], a[p] = a[p], a[c]
        for r in range(3):
            if r != c:
                f = a[r][c] / a[c][c]
                a[r] = [x - f * y for x, y in zip(a[r], a[c])]
    return [a[i][3] / a[i][i] for i in range(3)]


def adjusted_effect(y: list, grp: list, x: list) -> Optional[dict]:
    """Разница группы (grp = 1 для A) при равном x: МНК y = c0 + eff·grp + k·(x − x̄). {eff, se, lo, hi, sd_res, slope} или None."""
    n = len(y)
    if n < 5 or sum(grp) < 2 or n - sum(grp) < 2:
        return None
    xm = sum(x) / n
    X = [[1.0, float(g), xi - xm] for g, xi in zip(grp, x)]
    xtx = [[sum(r[i] * r[j] for r in X) for j in range(3)] for i in range(3)]
    coef = _solve3(xtx, [sum(r[i] * yi for r, yi in zip(X, y)) for i in range(3)])
    inv1 = _solve3(xtx, [0.0, 1.0, 0.0])                            # 2-й столбец обратной матрицы — дисперсия коэффициента группы
    if coef is None or inv1 is None:
        return None
    dof = n - 3
    sig2 = sum((yi - sum(c * xi for c, xi in zip(coef, r))) ** 2 for r, yi in zip(X, y)) / dof
    se = math.sqrt(max(0.0, sig2 * inv1[1]))
    h = cv_store._t975(dof) * se
    return {"eff": round(coef[1], 2), "se": round(se, 2), "lo": round(coef[1] - h, 2), "hi": round(coef[1] + h, 2),
            "sd_res": round(math.sqrt(sig2), 2), "slope": round(coef[2], 4)}


def need_per_group(sd: Optional[float], diff: Optional[float]) -> Optional[int]:
    """Сколько варок на группу, чтобы поймать разницу diff при разбросе sd (95 % уверенность, 80 % шанс). Разницы нет — None."""
    if not sd or not diff:
        return None
    return math.ceil(2 * (Z_POWER * sd / abs(diff)) ** 2)


def _verdict(n_min: int, real: bool, need: Optional[int]) -> tuple[str, str]:
    if n_min < MIN_N:
        return "few", "мало варок — нужно хотя бы %d в каждой группе" % MIN_N
    if real:
        return "real", "разница реальная"
    if need is not None and n_min < need <= NEED_MAX:
        return "few", "мало варок — нужно ~%d в каждой группе" % need
    return "noise", "в пределах шума" + (" (такую разницу поймать — нужно ~%d варок на группу)" % need if need else "")


def compare_rows(boils: list, a: dict, b: dict) -> dict:
    """Сравнение групп по строкам отчёта (report.build → boils). a, b: {"from", "to", "water": True/False/None, "name"}."""
    pool = [x for x in boils if _eligible(x) and (_in_group(x, a) or _in_group(x, b))]
    lim = LONG_K * st.median([x["duration_min"] for x in pool]) if pool else None
    long_ = [x for x in pool if x["duration_min"] > lim]
    pool = [x for x in pool if x["duration_min"] <= lim]
    ga, gb = [x for x in pool if _in_group(x, a)], [x for x in pool if _in_group(x, b)]
    both = {x["t_start"] for x in ga} & {x["t_start"] for x in gb}
    out = {"ok": False, "a": {"spec": a, "boils": [x["start"] for x in ga]}, "b": {"spec": b, "boils": [x["start"] for x in gb]}, "warnings": [],
           "excluded": [{"start": x["start"], "duration_min": x["duration_min"]} for x in long_], "long_min": round(lim, 1) if lim else None}
    if both:
        out["error"] = "группы пересекаются: %d варок попали в обе — разведите периоды или воду" % len(both)
        return out
    models = sorted({x["model"] for x in ga + gb if x.get("model")})
    unknown = sum(1 for x in ga + gb if not x.get("model"))
    out["models"], out["unknown_model"] = models, unknown
    if len(models) > 1:
        out["error"] = ("в группах разные модели распознавания (%s): модель меняет абсолютные размер и муку, сравнивать нельзя — "
                        "выберите периоды на одной модели" % ", ".join(models))
        return out
    if unknown and models:          # паспорт модели в пробах появился вместе с 1280 на Кире: варки без него — скорее всего, старая модель
        out["warnings"].append("у %d варок модель распознавания не записана, у остальных — %s: варки без записи, вероятно, сняты другой (старой) "
                               "моделью — такому сравнению не доверять, возьмите периоды, где модель записана" % (unknown, models[0]))
    elif unknown:
        out["warnings"].append("модель распознавания у варок не записана (пробы до 1.9.76) — убедитесь, что весь период на одной модели")
    for x in ga + gb:
        x["grow_pct"] = round(100.0 * (x["fin_um"] / x["point_um"] - 1), 2) if x.get("point_um") else None
    rows = []
    for key, name, adj in METRICS:
        va = [x[key] for x in ga if x.get(key) is not None]
        vb = [x[key] for x in gb if x.get(key) is not None]
        sa, sb = _stats(va), _stats(vb)
        diff = round(sa["mean"] - sb["mean"], 2) if va and vb else None
        p = perm_p(va, vb) if va and vb else None
        n_min = min(len(va), len(vb))
        sd = math.sqrt(((sa["sd"] or 0) ** 2 + (sb["sd"] or 0) ** 2) / 2) if sa["sd"] is not None and sb["sd"] is not None else None
        row = {"key": key, "name": name, "a": sa, "b": sb, "diff": diff, "p": round(p, 4) if p is not None else None, "adj": None}
        row["verdict"], row["text"] = _verdict(n_min, p is not None and p < P_REAL, need_per_group(sd, diff))
        if adj:
            in_a = {id(x) for x in ga}
            sel = [(x[key], 1 if id(x) in in_a else 0, x["point_um"]) for x in ga + gb if x.get(key) is not None]
            e = adjusted_effect([s[0] for s in sel], [s[1] for s in sel], [s[2] for s in sel])
            row["adj"] = e
            if e is not None:
                real = e["lo"] > 0 or e["hi"] < 0
                row["adj"]["verdict"], row["adj"]["text"] = _verdict(n_min, real, need_per_group(e["sd_res"], e["eff"]))
        rows.append(row)
    out.update(ok=True, metrics=rows)
    return out


def build(serial: str, a: dict, b: dict) -> dict:
    """Сравнение групп по журналу серийника. Границы групп по умолчанию — вся история."""
    now = time.time()
    for g in (a, b):
        g["from"] = g.get("from") if g.get("from") is not None else 0.0
        g["to"] = g.get("to") if g.get("to") is not None else now
    rep = report.build(serial, min(a["from"], b["from"]), max(a["to"], b["to"]))
    out = compare_rows(rep["boils"], a, b)
    out.update(serial=serial, point_sv=rep["point_sv"], generated=now)
    return out
