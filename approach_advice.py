"""approach_advice — советы по подводу микроскопа от разломов (этап «только предлагаем»: мотор и СВ-таблицу не трогаем).

Каждая проба в журнале (db.journal) помнит строку СВ-таблицы, по которой шёл подвод (ap_row), её SP (ap_sp, мкм) и датчики на
приходе (ap_ai — аналог 1271, ap_enc, ap_calc), плюс число подтверждённых разломов (frac_zones). Здесь пробы по строкам таблицы
сводятся в «совет»: если на строке был разлом — зазор SP этой строки стоит увеличить на шаг (подвод чуть дальше от стекла), но не выше
предельного зазора max_gap_um. В расчёт берутся только пробы при ТЕКУЩЕМ SP строки: поправили таблицу — счёт по строке начинается заново.

Чистая функция без обращений к диску и ПЛК — проверяется в tests/test_approach_advice.py и selfcheck.
"""
from __future__ import annotations

SP_EPS = 0.5          # мкм: SP пробы и текущий SP строки считаются равными


def _mean(vals):
    v = [x for x in vals if isinstance(x, (int, float))]
    return round(sum(v) / len(v), 1) if v else None


def compute(rows: list[dict], SP: list, SVSP: list, step_um: float = 100, min_zones: int = 1, max_gap_um: float = 1500) -> dict:
    """rows — строки журнала (с полями ap_*, frac_zones, sv). Возвращает {"rows": [...по строкам таблицы...], "step_um", "max_gap_um"}.

    Строка результата: row (индекс таблицы), sv_from, sp (текущий), n (проб), n_frac (с разломом), share (доля разломных),
    ai_frac / ai_ok (средний аналог на разломных и на чистых), status: "none" (проб нет) | "ok" (разломов нет) | "advice" |
    "ceiling" (разлом есть, но SP уже на предельном зазоре), proposed (предложенный SP или None), last_frac_ts."""
    step = float(step_um)
    cap = float(max_gap_um)
    need = max(1, int(min_zones))
    by_row: dict[int, list[dict]] = {}
    for r in rows:
        i = r.get("ap_row")
        if i is None:
            continue
        i = int(i)
        if not (1 <= i < len(SP)) or not SP[i] or not (i < len(SVSP) and SVSP[i] > 0):
            continue
        sp_then = r.get("ap_sp")
        if sp_then is not None and abs(float(sp_then) - float(SP[i])) > SP_EPS:
            continue                       # проба при другом SP — к текущей настройке строки не относится
        by_row.setdefault(i, []).append(r)

    out = []
    for i in range(1, len(SP)):
        if not (i < len(SVSP) and SVSP[i] > 0 and SP[i]):
            continue
        g = by_row.get(i, [])
        fr = [r for r in g if int(r.get("frac_zones") or 0) >= need]
        ok = [r for r in g if int(r.get("frac_zones") or 0) == 0]
        sp = float(SP[i])
        if not g:
            status, prop = "none", None
        elif not fr:
            status, prop = "ok", None
        elif sp >= cap:
            status, prop = "ceiling", None
        else:
            status, prop = "advice", round(min(sp + step, cap))
        out.append({
            "row": i, "sv_from": SVSP[i], "sp": sp, "n": len(g), "n_frac": len(fr),
            "share": round(len(fr) / len(g), 2) if g else None,
            "ai_frac": _mean([r.get("ap_ai") for r in fr]), "ai_ok": _mean([r.get("ap_ai") for r in ok]),
            "status": status, "proposed": prop,
            "last_frac_ts": max((r.get("ts") or "" for r in fr), default=None),
        })
    return {"rows": out, "step_um": step, "max_gap_um": cap, "min_zones": need}
