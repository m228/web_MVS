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
  мелочь   = объём кристаллов мельче fines_um (эквивалентный диаметр), без сростков
  сростки  = отдельной строкой (другой тип кристалла)
"""
from __future__ import annotations

import math

MODELS = ("m1", "m2", "m3")
KINDS = ("fines", "agg", "total")
DEFAULTS = {"fines_um": 200.0, "k_thick": 0.88, "fines_from_sv": 88.0}


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


def kind_of(m, fines_um: float) -> str | None:
    """К какой доле относится кристалл: fines / agg / rest; None — вне статистики (обрезан, пузырь)."""
    if m.group in ("cut", "bubble"):
        return None
    if m.defect == "aggregate":
        return "agg"
    return "fines" if m.size_um < fines_um else "rest"


def empty_sums() -> dict:
    s = {"area": {k: 0.0 for k in KINDS}}
    for mod in MODELS:
        s[mod] = {k: 0.0 for k in KINDS}
    s["n"] = {k: 0 for k in KINDS}
    return s


def sums_for(measures: list, cv_cfg: dict | None, count_fines: bool = True) -> dict:
    """Суммы объёма/площади по кадру: мелочь, сростки, всё. Складываются между кадрами пробы.
    count_fines=False — мелочь не считаем (варка не дошла до нужного СВ): мелкие кристаллы идут в «остальные»,
    а в сумме стоит пометка fines_off — доля мелочи будет None."""
    cfg = volume_cfg(cv_cfg)
    s = empty_sums()
    if not count_fines:
        s["fines_off"] = True
    for m in measures:
        kind = kind_of(m, cfg["fines_um"] if count_fines else 0.0)
        if kind is None:
            continue
        vols = crystal_volumes(m.size_um, m.length_um, m.width_um, cfg["k_thick"])
        parts = {"area": area_um2(m.size_um), **vols}
        for key, val in parts.items():
            s[key]["total"] += val
            if kind in ("fines", "agg"):
                s[key][kind] += val
        s["n"]["total"] += 1
        if kind in ("fines", "agg"):
            s["n"][kind] += 1
    return s


def add_sums(a: dict, b: dict) -> dict:
    """Сумма двух наборов (кадры одной пробы складываем, а не усредняем проценты)."""
    out = empty_sums()
    for key in ("area",) + MODELS:
        for k in KINDS:
            out[key][k] = (a.get(key) or {}).get(k, 0.0) + (b.get(key) or {}).get(k, 0.0)
    for k in KINDS:
        out["n"][k] = (a.get("n") or {}).get(k, 0) + (b.get("n") or {}).get(k, 0)
    if a.get("fines_off") or b.get("fines_off"):
        out["fines_off"] = True
    return out


def percents(sums: dict | None) -> dict:
    """Доли в % от общего: {m1:{fines,agg}, m2:…, m3:…, area:…, n:{fines,agg}}. Нет данных → None."""
    if not sums:
        return {}
    out = {}
    for key in ("area",) + MODELS:
        tot = (sums.get(key) or {}).get("total", 0.0)
        out[key] = ({k: round(100.0 * sums[key][k] / tot, 3) for k in ("fines", "agg")} if tot > 0
                    else {"fines": None, "agg": None})
    n_tot = (sums.get("n") or {}).get("total", 0)
    out["n"] = ({k: round(100.0 * sums["n"][k] / n_tot, 2) for k in ("fines", "agg")} if n_tot
                else {"fines": None, "agg": None})
    if sums.get("fines_off"):            # мелочь пока не считаем — прочерк, а не 0 %
        for key in out:
            out[key]["fines"] = None
    return out
