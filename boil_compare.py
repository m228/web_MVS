"""boil_compare — сравнение варок маточника по варке целиком (от заводки до готовности): две группы (опыт / контроль, «после / до
изменения») или одна варка против обычных. Что считать «лучше» — tasks/seed-quality-criteria.md.

Группа = период и/или вода да/нет. Варка входит в группу, только если она ЦЕЛИКОМ внутри периода: шедшая в момент изменения не идёт
ни в одну группу (список — в «excluded»). Короткие (оборвали) и затянутые (длиннее LONG_K медиан: простой, слившиеся варки) тоже не идут.
Показатели (report.build → cv_store.boil_metrics): сростки, мука, однородность (разброс размера в пробе), размер на финише, заводка →
готовность (у Буи «готовности» в пробах нет — заводка → последняя проба), длительность; и разброс размера МЕЖДУ варками.
По каждому: число варок, медиана, среднее, σ, разница средних A − B и p перестановочного теста (на малых группах — полный перебор,
иначе случайные перестановки с фиксированным зерном: ответ не прыгает от запроса к запросу). Тест по средним, не по медианам: на 4–6
варках медиана почти не двигается между разбиениями и тест слепнет (разделённые группы 6 против 10 давали p 0,12). Разброс между
варками — тот же тест по отклонениям от медианы группы (Браун — Форсайт).
Вердикт строки: «разница реальная» / «мало варок — нужно ~N» / «в пределах шума». Итог фразой — по правилам (summary): качество важнее
времени. Разные модели распознавания в группах — сравнение запрещено: модель меняет абсолютные цифры размера и муки (Кир 16.10, 640 → 1280).
Контрольной точки СВ (86) здесь нет: у Буи её нет вовсе, а на Кире изменения теперь в начале варки — поправка «при равном размере на
точке» съела бы сам эффект.
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
SPREAD_N = 8        # разброс МЕЖДУ варками по меньшим группам не поймать
P_REAL = 0.05
PERM_N = 20000      # перестановок, если полный перебор длиннее
NEED_MAX = 40       # нужно больше варок на группу (≈ 4 дня варок Кира) — разница мала, «в пределах шума»
Z_POWER = 2.8       # 1,96 + 0,84: 95 % уверенность, 80 % шанс поймать разницу такого размера
LONG_K = report.LONG_K      # варка длиннее медианы обеих групп во столько раз — затянута (простой, две варки слились), в сравнение не идёт
AROUND_MAX_S = 14 * 86400   # группа «до» по умолчанию — не длиннее двух недель
ONE_REF_N = 20      # «одна варка»: обычные — столько предыдущих типичных варок той же модели
ONE_EDGE = 10       # «одна варка»: вне 10–90 % обычных — хуже / лучше обычного
CURVE_PRE = 5.0     # кривая роста — с этой минуты до заводки
CURVE_STEP = 2.0    # шаг медианной кривой группы, мин

# (ключ, название, единица, знаков, как оценивать): lower — качество, меньше лучше; size — коридор цели; time — короче лучше, но после качества
METRICS = [("fin_agg_pct", "сростки на финише", "%", 1, "lower"),
           ("fines_avg", "мука на финише", "%", 1, "lower"),
           ("fin_cv", "однородность: разброс размера в пробе", "%", 1, "lower"),
           ("fin_um", "размер на финише", "мкм", 0, "size"),
           ("seed_ready_min", "заводка → готовность", "мин", 0, "time"),
           ("duration_min", "длительность варки", "мин", 0, "time")]
SPREAD = ("fin_um_spread", "разброс размера между варками", "%", 1, "lower")
SEED_END = ("seed_end_min", "заводка → последняя проба", "мин", 0, "time")
# слова итога: (лучше, хуже) для показателей качества
_WORDS = {"fin_agg_pct": ("сростков меньше", "сростков больше"), "fines_avg": ("муки меньше", "муки больше"),
          "fin_cv": ("кристалл однороднее", "кристалл разнороднее"),
          "fin_um_spread": ("размер от варки к варке стабильнее", "размер от варки к варке гуляет сильнее")}


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
    return bool(b.get("finished")) and b.get("fin_um") is not None and b.get("flag") != "short"


def _inside(b: dict, g: dict) -> bool:
    """Варка целиком в периоде группы и вода совпадает."""
    return g["from"] <= b["t_start"] and b["t_end"] <= g["to"] and (g.get("water") is None or b.get("water") is g["water"])


def _straddles(b: dict, g: dict) -> bool:
    """Варка шла на границе периода группы — началась до неё, кончилась после."""
    return any(b["t_start"] < x < b["t_end"] for x in (g["from"], g["to"]))


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


def _metric_row(spec: tuple, ga: list, gb: list) -> Optional[dict]:
    key, name, unit, nd, kind = spec
    pa = [[x["start"], x[key]] for x in ga if x.get(key) is not None]
    pb = [[x["start"], x[key]] for x in gb if x.get(key) is not None]
    if not pa and not pb:
        return None
    va, vb = [v for _, v in pa], [v for _, v in pb]
    sa, sb = _stats(va), _stats(vb)
    diff = round(sa["mean"] - sb["mean"], 2) if va and vb else None
    p = perm_p(va, vb) if va and vb else None
    sd = math.sqrt(((sa["sd"] or 0) ** 2 + (sb["sd"] or 0) ** 2) / 2) if sa["sd"] is not None and sb["sd"] is not None else None
    need = need_per_group(sd, diff)
    row = {"key": key, "name": name, "unit": unit, "nd": nd, "kind": kind, "a": sa, "b": sb, "diff": diff, "p": round(p, 4) if p is not None else None,
           "need": need, "pts": {"a": pa, "b": pb}}
    row["verdict"], row["text"] = _verdict(min(len(va), len(vb)), p is not None and p < P_REAL, need)
    return row


def _spread_row(va: list, vb: list) -> dict:
    """Разброс размера МЕЖДУ варками: σ / среднее, % по группе; тест — перестановки по |x − медиана группы| (Браун — Форсайт)."""
    def cv(v):
        return round(100.0 * st.stdev(v) / st.fmean(v), 1) if len(v) >= 2 and st.fmean(v) else None
    key, name, unit, nd, kind = SPREAD
    ca, cb = cv(va), cv(vb)
    row = {"key": key, "name": name, "unit": unit, "nd": nd, "kind": kind, "a": {"n": len(va), "mean": ca, "median": ca},
           "b": {"n": len(vb), "mean": cb, "median": cb}, "diff": round(ca - cb, 1) if ca is not None and cb is not None else None,
           "p": None, "need": SPREAD_N, "pts": None}
    if min(len(va), len(vb)) < SPREAD_N:
        row["verdict"], row["text"] = "few", "мало варок — разброс ловится от %d варок в каждой группе" % SPREAD_N
        return row
    p = perm_p([abs(x - st.median(va)) for x in va], [abs(x - st.median(vb)) for x in vb])
    row["p"] = round(p, 4)
    row["verdict"], row["text"] = ("real", "разница реальная") if p < P_REAL else ("noise", "в пределах шума")
    return row


def _norms(rows: list, targets: dict) -> None:
    """Цели маточника по медиане группы: мука выше порога, размер вне коридора → row["off"] = {"a": bool, "b": bool}."""
    for r in rows:
        if r["key"] == "fines_avg":
            r["off"] = {g: r[g]["median"] is not None and r[g]["median"] > targets["fines_max"] for g in ("a", "b")}
        elif r["key"] == "fin_um":
            r["off"] = {g: r[g]["median"] is not None and not targets["size_min_um"] <= r[g]["median"] <= targets["size_max_um"] for g in ("a", "b")}


def _fmt(v, nd: int = 1) -> str:
    return "—" if v is None else ("%.*f" % (nd, v)).replace(".", ",").replace("-", "−")


def summary(rows: list, mode: str = "groups", targets: Optional[dict] = None) -> dict:
    """Итог сравнения фразой по правилам tasks/seed-quality-criteria.md, без ИИ: одни и те же цифры — всегда одна и та же фраза.
    Качество (сростки, мука, однородность, разброс между варками, порог муки, коридор размера) важнее времени: хоть один показатель
    качества реально хуже — «в минус», даже если варка короче. Мука и размер сдвинулись вместе — одна строка (у нас это почти один
    сигнал, r −0,93). verdict: plus / minus / mixed (лучше, но дольше) / few / none. mode «change» — слова про изменение (А после, Б до),
    иначе — про группу А против Б (две свободные группы: опыт / контроль)."""
    tg = targets or report.SEED_TARGET
    who = "после изменения" if mode == "change" else "в группе А"
    by = {r["key"]: r for r in rows}
    real = lambda k: k in by and by[k]["verdict"] == "real" and by[k]["diff"]       # noqa: E731
    plus, minus, lines = [], [], []

    def ab(r):
        return "%s против %s %s" % (_fmt(r["a"]["mean"], r["nd"]), _fmt(r["b"]["mean"], r["nd"]), r["unit"])
    fines_up = real("fines_avg") and by["fines_avg"]["diff"] > 0
    pair = real("fines_avg") and real("fin_um") and (by["fin_um"]["diff"] > 0) != fines_up        # крупнее и муки меньше (или наоборот)
    for k in ("fin_agg_pct", "fines_avg", "fin_cv", "fin_um_spread"):
        if real(k):
            good = by[k]["diff"] < 0
            word = _WORDS[k][0 if good else 1]
            if k == "fines_avg" and pair:
                word = "кристалл крупнее и муки меньше" if good else "кристалл мельче и муки больше"
                lines.append("%s (это один сигнал): %s, %s" % (word, ab(by["fin_um"]), ab(by[k])))
            else:
                lines.append("%s: %s" % (word, ab(by[k])))
            (plus if good else minus).append(word)
    if real("fin_um") and not pair:
        lines.append("кристалл %s: %s" % ("крупнее" if by["fin_um"]["diff"] > 0 else "мельче", ab(by["fin_um"])))
    for k, bad, back in (("fines_avg", "мука выше порога %s %%" % _fmt(tg["fines_max"], 0), "мука вернулась под порог %s %%" % _fmt(tg["fines_max"], 0)),
                         ("fin_um", "размер вне коридора %s–%s мкм" % (_fmt(tg["size_min_um"], 0), _fmt(tg["size_max_um"], 0)),
                          "размер вернулся в коридор %s–%s мкм" % (_fmt(tg["size_min_um"], 0), _fmt(tg["size_max_um"], 0)))):
        off = (by.get(k) or {}).get("off")
        if not off:
            continue
        med = _fmt(by[k]["a"]["median"], by[k]["nd"])
        if off["a"] and not off["b"]:
            lines.append("%s %s: медиана %s" % (bad, who, med))
            minus.append(bad)
        elif off["b"] and not off["a"]:
            lines.append("%s: медиана %s" % (back, med))
            plus.append(back)
        elif off["a"]:
            lines.append("%s в обеих группах" % bad)
    tk = "seed_ready_min" if "seed_ready_min" in by else ("seed_end_min" if "seed_end_min" in by else "duration_min")
    tmin = round(by[tk]["diff"]) if real(tk) and round(by[tk]["diff"]) else 0
    if tmin:
        lines.append("варка %s на %d мин (%s)" % ("короче" if tmin < 0 else "дольше", abs(tmin), by[tk]["name"]))
    few = [r for r in rows if r["kind"] != "time" and r["verdict"] == "few"]
    if few:
        need = max([r["need"] for r in few if r.get("need")] or [MIN_N])
        lines.append("мало варок, чтобы судить про %s: нужно ~%d в каждой группе" % (", ".join(r["name"] for r in few), need))
    calm = [r["name"] for r in rows if r["verdict"] == "noise"]
    if calm:
        lines.append("без разницы: " + ", ".join(calm))
    # слова итога: «до / после» — про изменение, две свободные группы (опыт / контроль, вода / без воды) — про группу А против Б
    w = ({"minus": "изменение в минус — ", "mixed": "лучше по качеству", "plus": "изменение в плюс — ", "faster": "в плюс — варка короче на %d мин при том же качестве",
          "slower": "варка дольше на %d мин при том же качестве — изменение не окупается"} if mode == "change" else
         {"minus": "А хуже Б — ", "mixed": "А лучше Б по качеству", "plus": "А лучше Б — ", "faster": "А лучше Б — варка короче на %d мин при том же качестве",
          "slower": "А хуже Б — варка дольше на %d мин при том же качестве"})
    if minus:
        verdict, text = "minus", w["minus"] + ", ".join(minus) + ("; лучше: " + ", ".join(plus) if plus else "") + \
            ("; варка при этом короче на %d мин" % -tmin if tmin < 0 else "; и варка дольше на %d мин" % tmin if tmin > 0 else "")
    elif plus and tmin > 0:
        verdict, text = "mixed", "%s (%s), но варка дольше на %d мин — решать технологу" % (w["mixed"], ", ".join(plus), tmin)
    elif plus:
        verdict, text = "plus", w["plus"] + ", ".join(plus) + (", и варка короче на %d мин" % -tmin if tmin < 0 else "")
    elif tmin < 0:
        verdict, text = "plus", w["faster"] % -tmin
    elif tmin > 0:
        verdict, text = "minus", w["slower"] % tmin
    elif [r for r in few if r["key"] != "fin_um_spread"]:          # разброс между варками ловится только от 8 варок — итог им не держим
        verdict, text = "few", "пока не сказать — мало варок"
    else:
        verdict, text = "none", "разницы не видно"
    caveat = ("Сравнение «до / после»: если в это же время меняли что-то ещё (сырьё, другой параметр), эффект общий — по отдельности не разделить."
              if mode == "change" else None)
    return {"verdict": verdict, "text": "Итог: " + text + ".", "lines": lines, "caveat": caveat}


def compare_rows(boils: list, a: dict, b: dict, targets: Optional[dict] = None, mode: str = "groups") -> dict:
    """Сравнение групп по строкам отчёта (report.build → boils). a, b: {"from", "to", "water": True/False/None}. targets — цели маточника
    (report.seed_target); mode «change» — А после изменения, Б до (слова итога)."""
    targets = targets or report.SEED_TARGET
    cand = [x for x in boils if bool(x.get("finished")) and x.get("fin_um") is not None]
    short = [x for x in cand if x.get("flag") == "short"]
    cand = [x for x in cand if _eligible(x)]
    pool = [x for x in cand if _inside(x, a) or _inside(x, b)]
    edge = [x for x in cand if x not in pool and (_straddles(x, a) or _straddles(x, b))]
    short = [x for x in short if _inside(x, a) or _inside(x, b)]
    lim = LONG_K * st.median([x["duration_min"] for x in pool]) if pool else None
    long_ = [x for x in pool if x["duration_min"] > lim]
    pool = [x for x in pool if x["duration_min"] <= lim]
    ga, gb = [x for x in pool if _inside(x, a)], [x for x in pool if _inside(x, b)]
    both = {x["t_start"] for x in ga} & {x["t_start"] for x in gb}
    ex = lambda xs, why: [{"start": x["start"], "duration_min": x["duration_min"], "why": why} for x in xs]      # noqa: E731
    out = {"ok": False, "mode": mode, "a": {"spec": a, "boils": [x["start"] for x in ga]}, "b": {"spec": b, "boils": [x["start"] for x in gb]},
           "warnings": [], "excluded": ex(edge, "edge") + ex(long_, "long") + ex(short, "short"), "long_min": round(lim, 1) if lim else None,
           "targets": targets}
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
    # время от заводки: у Буи «готовность» (стадия 9) в пробы почти не попадает — тогда до последней пробы, одинаково для всех варок
    have_end = [x for x in ga + gb if x.get("seed_end_min") is not None]
    seed_spec = METRICS[4] if len([x for x in have_end if x.get("seed_ready_min") is not None]) >= 0.8 * len(have_end) else SEED_END
    rows = []
    for spec in METRICS[:4] + [seed_spec, METRICS[5]]:
        row = _metric_row(spec, ga, gb)
        if row is None:
            continue
        rows.append(row)
        if spec[0] == "fin_um":
            rows.append(_spread_row([v for _, v in row["pts"]["a"]], [v for _, v in row["pts"]["b"]]))
    _norms(rows, targets)
    out.update(ok=True, metrics=rows, summary=summary(rows, mode, targets))
    return out


# --- ход размера: кривые от заводки ---
def _series(g: list, t0: float, end_t: float) -> list:
    """[[мин от t0, мкм]] по пробам варки до end_t (готовность или последняя проба), сглажено по 3 соседним пробам."""
    pts = [((r["t"] - t0) / 60.0, r["mean"]) for r in g if r.get("mean") is not None and r["t"] <= end_t + 1]
    out = []
    for i in range(len(pts)):
        w = [y for _, y in pts[max(0, i - 1):i + 2]]
        out.append([round(pts[i][0], 1), round(sum(w) / len(w), 1)])
    return [p for p in out if p[0] >= -CURVE_PRE]


def _at(s: list, x: float) -> Optional[float]:
    """Значение кривой в минуте x (линейно между пробами); вне кривой — None."""
    for (x0, y0), (x1, y1) in zip(s, s[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 > x0 else y0
    return None


def _band(series: list, until: float) -> list:
    """Медиана группы по минутам и «обычно» (25–75 %): [[мин, медиана, q1, q3]] там, где есть хотя бы половина кривых (и не меньше 2)."""
    out, x = [], -CURVE_PRE
    while x <= until + 1e-9:
        v = sorted(y for y in (_at(s, x) for s in series if len(s) >= 2) if y is not None)
        if len(v) >= max(2, len(series) / 2):
            out.append([round(x, 1), round(st.median(v), 1), report._quantile(v, 0.25), report._quantile(v, 0.75)])
        x += CURVE_STEP
    return out


def _curve(g: list, row: dict) -> dict:
    """Кривая варки от заводки (нет заводки — от первой пробы, from_seed False) до готовности или последней пробы."""
    t0 = row.get("seed_t") or g[0]["t"]
    end = t0 + 60 * row["seed_ready_min"] if row.get("seed_t") and row.get("seed_ready_min") else g[-1]["t"]
    return {"start": row["start"], "from_seed": bool(row.get("seed_t")), "end": round((end - t0) / 60.0, 1), "pts": _series(g, t0, end)}


def _curves(groups: list, rows: list, starts: list) -> dict:
    by_ts = {g[0]["ts"]: g for g in groups}
    cur = [_curve(by_ts[r["start"]], r) for r in rows if r["start"] in starts and r["start"] in by_ts]
    until = st.median([c["end"] for c in cur]) if cur else 0
    return {"boils": cur, "band": _band([c["pts"] for c in cur], until), "end": round(until, 1)}


def build(serial: str, a: dict, b: dict, mode: str = "groups") -> dict:
    """Сравнение групп по журналу серийника. Границы групп по умолчанию — вся история."""
    now = time.time()
    for g in (a, b):
        g["from"] = g.get("from") if g.get("from") is not None else 0.0
        g["to"] = g.get("to") if g.get("to") is not None else now
    groups = cv_store.boil_groups(serial)
    rep = report.build(serial, min(a["from"], b["from"]), max(a["to"], b["to"]), groups=groups)
    out = compare_rows(rep["boils"], a, b, rep["targets"], mode)
    out.update(serial=serial, generated=now, changes=report.changes(serial, min(a["from"], b["from"]), max(a["to"], b["to"])))
    if out["ok"]:
        out["curves"] = {k: _curves(groups, rep["boils"], out[k]["boils"]) for k in ("a", "b")}
    return out


def around(serial: str, t: float, now: Optional[float] = None) -> dict:
    """Группы по умолчанию для изменения в момент t: А — от t до следующего изменения (или до сейчас), Б — от прошлого изменения до t,
    но не раньше смены модели распознавания и не длиннее AROUND_MAX_S. Изменения, между которыми не уместилась ни одна целая варка
    (19.10: подкачка 40 % в 11:20, рецепт 2 в 12:00), — одно изменение: Б кончается на первом из них, в ответе — together."""
    now = time.time() if now is None else now
    ch = sorted(c["t"] for c in report.changes(serial))
    nxt = min([x for x in ch if x > t + 60], default=None)
    groups = cv_store.boil_groups(serial)
    rows = report.build(serial, t - AROUND_MAX_S - 86400, t, groups=groups)["boils"]
    b_to, together = t, []
    while True:
        prv = max([x for x in ch if x < b_to - 60], default=None)
        if prv is None or any(prv <= r["t_start"] and r["t_end"] <= b_to for r in rows if r.get("finished")):
            break
        together.append(prv)                # между prv и b_to целой варки нет — это одно изменение
        b_to = prv
    b_from = max(x for x in (prv, t - AROUND_MAX_S) if x is not None)
    segs = report._segments([r for r in rows if b_from <= r["t_start"] <= b_to])
    if len(segs) > 1:                       # модель меняли внутри «до» — Б только с последнего отрезка
        first = next((r for r in rows if r["start"] == segs[-1]["from"]), None)
        if first is not None:
            b_from = first["t_start"]
    return {"a_from": t, "a_to": nxt if nxt is not None else now, "b_from": b_from, "b_to": b_to, "next_change": nxt, "prev_change": prv,
            "together": together}


def _place(v: Optional[float], ref: list) -> Optional[float]:
    """Место значения среди обычных, %: доля обычных варок ниже (половина равных — посередине)."""
    if v is None or not ref:
        return None
    return round(100.0 * (sum(1 for x in ref if x < v) + 0.5 * sum(1 for x in ref if x == v)) / len(ref), 0)


def one(serial: str, boil: str, ref_from: Optional[float] = None, ref_to: Optional[float] = None) -> dict:
    """Одна варка против обычных: где она среди типичных варок той же модели. Обычные по умолчанию — ONE_REF_N предыдущих;
    с периодом — все типичные варки периода, кроме неё самой."""
    groups = cv_store.boil_groups(serial)
    g = next((x for x in groups if x[0]["ts"] == boil), None)
    if g is None:
        return {"ok": False, "error": "варка %s не найдена в журнале" % boil}
    t0 = ref_from if ref_from is not None else g[0]["t"] - 30 * 86400
    t1 = ref_to if ref_to is not None else g[-1]["t"]
    rep = report.build(serial, min(t0, g[0]["t"]), max(t1, g[0]["t"]), groups=groups)
    me = next((x for x in rep["boils"] if x["start"] == boil), None)
    if me is None:
        return {"ok": False, "error": "варка %s слишком короткая для отчёта" % boil}
    ref = [x for x in rep["boils"] if x["start"] != boil and not x.get("flag") and x.get("fin_um") is not None and x.get("model") == me.get("model")
           and t0 <= x["t_start"] and x["t_end"] <= t1]
    if ref_from is None:
        ref = [x for x in ref if x["t_start"] < me["t_start"]][-ONE_REF_N:]
    tg = rep["targets"]
    seed_spec = METRICS[4] if me.get("seed_ready_min") is not None else SEED_END
    rows, worse, better, lines = [], [], [], []
    for key, name, unit, nd, kind in METRICS[:4] + [seed_spec, METRICS[5]]:
        v = me.get(key)
        rv = sorted(x[key] for x in ref if x.get(key) is not None)
        if v is None and not rv:
            continue
        pl = _place(v, rv)
        row = {"key": key, "name": name, "unit": unit, "nd": nd, "kind": kind, "value": v, "n": len(rv), "place": pl,
               "median": report._quantile(rv, 0.5), "q1": report._quantile(rv, 0.25), "q3": report._quantile(rv, 0.75)}
        if v is None or len(rv) < 2 * MIN_N:
            row["verdict"] = "few"
        elif kind == "size":
            row["verdict"] = "high" if pl >= 100 - ONE_EDGE else ("low" if pl <= ONE_EDGE else "usual")
        else:
            row["verdict"] = "worse" if pl >= 100 - ONE_EDGE else ("better" if pl <= ONE_EDGE else "usual")
        u = " %" if unit == "%" else " " + unit
        usual = "обычно %s–%s%s" % (_fmt(row["q1"], nd), _fmt(row["q3"], nd), u)
        if row["verdict"] in ("worse", "better"):
            (worse if row["verdict"] == "worse" else better).append("%s %s%s (%s)" % (name, _fmt(v, nd), u, usual))
        elif row["verdict"] in ("high", "low"):
            lines.append("кристалл %s обычного: %s%s (%s)" % ("крупнее" if row["verdict"] == "high" else "мельче", _fmt(v, nd), u, usual))
        rows.append(row)
    if me.get("fines_avg") is not None and me["fines_avg"] > tg["fines_max"]:
        worse.append("мука выше порога %s %%" % _fmt(tg["fines_max"], 0))
    if me.get("fin_um") is not None and not tg["size_min_um"] <= me["fin_um"] <= tg["size_max_um"]:
        worse.append("размер вне коридора %s–%s мкм" % (_fmt(tg["size_min_um"], 0), _fmt(tg["size_max_um"], 0)))
    if worse:
        text = "хуже обычного — " + "; ".join(worse) + ("; лучше: " + "; ".join(better) if better else "")
    elif better:
        text = "лучше обычного — " + "; ".join(better)
    elif len(ref) < 2 * MIN_N:
        text = "мало обычных варок для сравнения (%d)" % len(ref)
    else:
        text = "как обычно"
    by_ts = {x[0]["ts"]: x for x in groups}
    ref_c = [_curve(by_ts[x["start"]], x) for x in ref if x["start"] in by_ts]
    mine = _curve(g, me)
    return {"ok": True, "serial": serial, "boil": me, "metrics": rows, "targets": tg, "model": me.get("model"),
            "ref": {"n": len(ref), "from": ref[0]["start"] if ref else None, "to": ref[-1]["start"] if ref else None},
            "summary": {"text": "Итог: " + text + ".", "lines": lines},
            "curves": {"me": mine, "band": _band([c["pts"] for c in ref_c], max([c["end"] for c in ref_c] + [mine["end"]]))},     # полоса кончается сама там, где кривых меньше половины
            "generated": time.time()}
