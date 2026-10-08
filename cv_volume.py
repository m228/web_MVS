"""cv_volume — объём и площадь кристаллов по 2D-кадру и доля мелочи по объёму.

Камера видит только проекцию, толщины на кадре нет, поэтому объём — это модель. Считаем три сразу
(Макс смотрит, какая ближе к жизни; к лаборатории подгоним коэффициент):

  M1 шар        V = π/6 · d³,           d = √(4·S/π)  — только площадь, без допущений о толщине
  M2 сфероид    V = π/6 · L · W · T,    T = k·W       — длина, ширина, толщина как доля ширины
  M3 призма     V = S · T,              T = k·W       — плоский кристалл, лежит на грани (рабочая)

S — площадь маски, L/W — длинная/короткая сторона minAreaRect, k — коэффициент толщины (cv.volume.k_thick).
По умолчанию k = 0,88: отношение осей c/b сахарозы 0,8782 : 1 (Сапронов, с. 294) — допущение, что
короткая сторона проекции ≈ ось b, а толщина ≈ ось c. Подгоняется по лаборатории.

Доли — от общего объёма всех кристаллов кадра (без обрезанных краем и пузырей):
  мелочь   = объём кристаллов с площадью меньше квадрата fines_side_mm × fines_side_mm (по умолчанию 0,2 × 0,2 мм =
             0,04 мм²), без сростков. Это то же, что эквивалентный диаметр меньше fines_um = 2·сторона/√π (≈ 226 мкм)
  сростки  = отдельной строкой (другой тип кристалла)
"""
from __future__ import annotations

import math

MODELS = ("m1", "m2", "m3")
# Рассев по ситам (как в лаборатории): отверстия, мм. Фракции снизу вверх: дно (<0,2), 0,2–0,5, 0,5–0,7, 0,7–0,8, 0,8–1,0, 1,0–1,2, >1,2.
# Размер кристалла — эквивалентный диаметр (как везде в CV); доля фракции — по объёму (объём ~ масса), % от всех кристаллов.
CALC_VER = 4     # 2: в расчёт идут только хорошие кристаллы (брак — сросток/игла/кривой — отсеян); менять при смене логики: старые пробы пересчитаются
SIEVE_MM = (0.2, 0.5, 0.7, 0.8, 1.0, 1.2)
SIEVE_LABELS = ("дно <0,2", "0,2–0,5", "0,5–0,7", "0,7–0,8", "0,8–1", "1–1,2", ">1,2")


def sieve_bin(size_um: float) -> int:
    """Номер фракции рассева: 0 — дно (мельче 0,2 мм), …, 6 — крупнее 1,2 мм."""
    d, i = size_um / 1000.0, 0
    while i < len(SIEVE_MM) and d >= SIEVE_MM[i]:
        i += 1
    return i
KINDS = ("fines", "agg", "total")
DEFAULTS = {"fines_side_mm": 0.2, "k_thick": 0.88, "fines_from_sv": 88.0, "avg_n": 4}    # avg_n — сколько последних проб финиша усредняем


def fines_diameter_um(side_mm: float) -> float:
    """Эквивалентный диаметр круга с площадью квадрата side_mm × side_mm, мкм: d = 2·сторона/√π."""
    return 2000.0 * side_mm / math.sqrt(math.pi)


def volume_cfg(cv_cfg: dict | None) -> dict:
    """Настройки объёма из блока cv (с дефолтами и защитой от мусора)."""
    v = dict(DEFAULTS)
    src = (cv_cfg or {}).get("volume") or {}
    for key in DEFAULTS:
        try:
            x = float(src.get(key, v[key]))
            if x > 0 or (key == "fines_from_sv" and x == 0):    # 0 — считать мелочь на любом СВ
                v[key] = x
        except (TypeError, ValueError):
            pass
    v["avg_n"] = max(1, int(round(v["avg_n"])))
    v["calc_ver"] = CALC_VER
    v["fines_um"] = fines_diameter_um(v["fines_side_mm"])    # расчётный порог по диаметру — им и сравниваем размер кристалла
    return v


def fines_on(cv_cfg: dict | None, sv) -> bool:
    """Считать ли мелочь: пока варка не дошла до «Мелочь с СВ», мелочь — неактуальная информация (кристаллы
    ещё растут) и не считается. СВ неизвестно или включено «брак всегда» — считаем (как у брака)."""
    if (cv_cfg or {}).get("reject_always") or sv is None:
        return True
    return float(sv) >= volume_cfg(cv_cfg)["fines_from_sv"]


def area_um2(size_um: float) -> float:
    """Площадь по эквивалентному диаметру (он и считался из площади маски)."""
    return math.pi * (size_um / 2.0) ** 2


def crystal_volumes(size_um: float, length_um: float, width_um: float, k: float) -> dict:
    """Объём одного кристалла по трём моделям, мкм³."""
    t = k * width_um
    return {
        "m1": math.pi / 6.0 * size_um ** 3,
        "m2": math.pi / 6.0 * length_um * width_um * t,
        "m3": area_um2(size_um) * t,
    }


REJECT_DEFECTS = ("aggregate", "needle", "crooked")     # брак по форме: в расчёт хороших кристаллов не идёт


def kind_of(m, fines_um: float) -> str | None:
    """К какой доле относится кристалл: fines / rest (хорошие — идут в расчёт и рассев) / agg (брак по форме, отсеян:
    считается отдельно, в общий объём не входит); None — вне статистики (обрезан, пузырь)."""
    if m.group in ("cut", "bubble"):
        return None
    if m.defect in REJECT_DEFECTS:
        return "agg"
    return "fines" if m.size_um < fines_um else "rest"


def empty_sums() -> dict:
    s = {"area": {k: 0.0 for k in KINDS}}
    for mod in MODELS:
        s[mod] = {k: 0.0 for k in KINDS}
    s["n"] = {k: 0 for k in KINDS}
    s["sieve"] = {mod: [0.0] * (len(SIEVE_MM) + 1) for mod in MODELS + ("area",)}   # объём (M1–M3, мкм³) и площадь ("area", мкм²) по фракциям рассева
    return s


def sums_for(measures: list, cv_cfg: dict | None, count_fines: bool = True) -> dict:
    """Суммы объёма/площади по кадру: мелочь, сростки, всё. Складываются между кадрами пробы.
    count_fines=False — мелочь не считаем (варка не дошла до нужного СВ): мелкие кристаллы идут в «остальные»,
    а в сумме стоит пометка fines_off — доля мелочи будет None."""
    cfg = volume_cfg(cv_cfg)
    s = empty_sums()
    if not count_fines:
        s["fines_off"] = True          # только флаг «СВ ниже порога» (для показа по пробе); саму муку считаем всегда —
                                       # порог СВ по варке применяется при показе (cv_store._apply_fines_gate)
    for m in measures:
        kind = kind_of(m, cfg["fines_um"])
        if kind is None:
            continue
        vols = crystal_volumes(m.size_um, m.length_um, m.width_um, cfg["k_thick"])
        parts = {"area": area_um2(m.size_um), **vols}
        if kind == "agg":                        # брак по форме отсеян: считаем отдельно, в общий объём и рассев не кладём
            for key, val in parts.items():
                s[key]["agg"] += val
            s["n"]["agg"] += 1
            continue
        for key, val in parts.items():
            s[key]["total"] += val
            if kind == "fines":
                s[key]["fines"] += val
        s["n"]["total"] += 1
        b = sieve_bin(m.size_um)
        for mod in MODELS:
            s["sieve"][mod][b] += vols[mod]
        s["sieve"]["area"][b] += parts["area"]
        if kind == "fines":
            s["n"]["fines"] += 1
    return s


def add_sums(a: dict, b: dict) -> dict:
    """Сумма двух наборов (кадры одной пробы складываем, а не усредняем проценты)."""
    out = empty_sums()
    for key in ("area",) + MODELS:
        for k in KINDS:
            out[key][k] = (a.get(key) or {}).get(k, 0.0) + (b.get(key) or {}).get(k, 0.0)
    for k in KINDS:
        out["n"][k] = (a.get("n") or {}).get(k, 0) + (b.get("n") or {}).get(k, 0)
    for mod in MODELS + ("area",):
        sa, sb = (a.get("sieve") or {}).get(mod), (b.get("sieve") or {}).get(mod)
        out["sieve"][mod] = [(sa[i] if sa else 0.0) + (sb[i] if sb else 0.0) for i in range(len(SIEVE_MM) + 1)]
    if a.get("fines_off") or b.get("fines_off"):
        out["fines_off"] = True
    return out


def percents(sums: dict | None, ignore_off: bool = False) -> dict:
    """Доли в % от общего: {m1:{fines,agg}, m2:…, m3:…, area:…, n:{fines,agg}}. Нет данных → None."""
    if not sums:
        return {}
    out = {}
    for key in ("area",) + MODELS:
        tot, rej = (sums.get(key) or {}).get("total", 0.0), (sums.get(key) or {}).get("agg", 0.0)
        # мука — доля среди ХОРОШИХ кристаллов; agg — доля отсеянного брака среди всех (хорошие + брак)
        out[key] = ({"fines": round(100.0 * sums[key]["fines"] / tot, 3), "agg": round(100.0 * rej / (tot + rej), 3)} if tot > 0
                    else {"fines": None, "agg": None})
    n_tot, n_rej = (sums.get("n") or {}).get("total", 0), (sums.get("n") or {}).get("agg", 0)
    out["n"] = ({"fines": round(100.0 * sums["n"]["fines"] / n_tot, 2), "agg": round(100.0 * n_rej / (n_tot + n_rej), 2)} if n_tot
                else {"fines": None, "agg": None})
    if sums.get("fines_off") and not ignore_off:            # мелочь пока не считаем — прочерк, а не 0 %
        for key in out:
            out[key]["fines"] = None
    # рассев: доля каждой фракции в общем объёме, % (по трём моделям); нет данных — не добавляем
    sv = {} if (sums.get("fines_off") and not ignore_off) else (sums.get("sieve") or {})     # рассев — как мука: только когда СВ дошёл до порога
    out["sieve"] = {mod: [round(100.0 * x / sum(sv[mod]), 3) for x in sv[mod]] for mod in MODELS + ("area",) if sv.get(mod) and sum(sv[mod]) > 0}
    return out
