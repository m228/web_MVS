"""approach_advice — советы по подводу микроскопа от разломов, в обе стороны (мотор и СВ-таблицу не трогаем — только предлагаем).

Каждая проба в журнале (db.journal) помнит строку СВ-таблицы, по которой шёл подвод (ap_row), её SP (ap_sp, мкм) и датчики на
приходе (ap_ai — аналог 1271, ap_enc, ap_calc), плюс разломы пробы (frac_zones, frac_pct — % площади). По строкам таблицы:

* «дальше» (status "advice") — при текущем SP строки была разломная проба → SP + шаг, не выше предельного зазора max_gap_um;
* «ближе» (status "closer") — при текущем SP подряд clean_n чистых проб и ни одной разломной → SP − шаг, не ниже min_gap_um.
  Ближе к стеклу — кристаллы ложатся в один слой, картинка однороднее; поэтому щупаем активно в обе стороны.

Разломная проба — зон ≥ min_zones и площадь ≥ weak_pct % (меньшая площадь — слабый след, проба считается чистой; 0 — выкл).
Защита от качелей: «ближе» не советуем, если на целевом зазоре у этой строки разломы уже были часто (≥ 2 разломных и ≥ половины
проб). Положение берём по аналоговому датчику (ap_ai), а не по заданию: подвод бывает перелетает (задание 1400, датчик 1300), и
разлом надо относить туда, где стекло реально стояло. Единичный разлом — случайность, «ближе» пробуем снова.

В расчёт статуса берутся только пробы при ТЕКУЩЕМ SP строки: поправили таблицу — счёт по строке начинается заново.
Чистая функция без обращений к диску и ПЛК — проверяется в tests/test_approach_advice.py и selfcheck.
"""
from __future__ import annotations

SP_EPS = 0.5          # мкм: SP пробы и текущий SP строки считаются равными
HOLD_MIN_FRAC = 2     # защита от качелей: столько разломных проб на целевом зазоре…
HOLD_SHARE = 0.5      # …и такая их доля — «ближе» туда не советуем


def _mean(vals):
    v = [x for x in vals if isinstance(x, (int, float))]
    return round(sum(v) / len(v), 1) if v else None


def is_frac(r: dict, min_zones: int = 1, weak_pct: float = 0) -> bool:
    """Проба разломная: зон не меньше порога и площадь не меньше «слабого» порога (%)."""
    if int(r.get("frac_zones") or 0) < max(1, int(min_zones)):
        return False
    return not weak_pct or float(r.get("frac_pct") or 0) >= float(weak_pct)


def _pos(r: dict):
    """Где реально стояло стекло: аналоговый датчик, если записан, иначе задание строки."""
    for k in ("ap_ai", "ap_sp"):
        v = r.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def compute(rows: list[dict], SP: list, SVSP: list, step_um: float = 50, min_zones: int = 1, max_gap_um: float = 1500,
            min_gap_um: float = 500, clean_n: int = 5, weak_pct: float = 0) -> dict:
    """rows — строки журнала (поля ap_*, frac_zones, frac_pct, ts). Возвращает {"rows": [...по строкам таблицы...], настройки}.

    Строка результата: row, sv_from, sp (текущий), n (проб при нём), n_frac (разломных), share, n_clean_tail (чистых подряд с конца),
    ai_frac / ai_ok (средний аналог на разломных / чистых), status, proposed (SP или None), last_frac_ts, hold (для "held").
    status: "none" — проб нет; "ok" — разломов нет, но чистых подряд меньше clean_n; "advice" — дальше; "closer" — ближе;
    "ceiling" — разлом, но SP уже на предельном зазоре; "floor" — чисто, но SP уже на минимальном; "held" — чисто, но на SP − шаг
    разломы были часто (качели)."""
    step = float(step_um)
    cap, low = float(max_gap_um), float(min_gap_um)
    need = max(1, int(min_zones))
    n_clean = max(1, int(clean_n))
    weak = float(weak_pct or 0)
    by_row: dict[int, list[dict]] = {}          # пробы при текущем SP строки
    hist: dict[int, list[dict]] = {}            # все пробы строки за окно (для защиты от качелей)
    for r in rows:
        i = r.get("ap_row")
        if i is None:
            continue
        i = int(i)
        if not (1 <= i < len(SP)) or not SP[i] or not (i < len(SVSP) and SVSP[i] > 0):
            continue
        hist.setdefault(i, []).append(r)
        sp_then = r.get("ap_sp")
        if sp_then is not None and abs(float(sp_then) - float(SP[i])) > SP_EPS:
            continue                       # проба при другом SP — к текущей настройке строки не относится
        by_row.setdefault(i, []).append(r)

    out = []
    for i in range(1, len(SP)):
        if not (i < len(SVSP) and SVSP[i] > 0 and SP[i]):
            continue
        g = sorted(by_row.get(i, []), key=lambda r: r.get("ts") or "")
        flags = [is_frac(r, need, weak) for r in g]
        fr = [r for r, f in zip(g, flags) if f]
        ok = [r for r, f in zip(g, flags) if not f]
        tail = 0
        for f in reversed(flags):
            if f:
                break
            tail += 1
        sp = float(SP[i])
        status, prop, hold = "none", None, None
        if fr:
            if sp >= cap:
                status = "ceiling"
            else:
                status, prop = "advice", round(min(sp + step, cap))
        elif g:
            if tail < n_clean:
                status = "ok"
            elif sp <= low:
                status = "floor"
            else:
                target = max(sp - step, low)
                near = [r for r in hist.get(i, []) if _pos(r) is not None and abs(_pos(r) - target) <= step / 2]
                nf = sum(1 for r in near if is_frac(r, need, weak))
                if nf >= HOLD_MIN_FRAC and nf >= HOLD_SHARE * len(near):
                    status, hold = "held", {"at": round(target), "n": len(near), "n_frac": nf}
                else:
                    status, prop = "closer", round(target)
        out.append({
            "row": i, "sv_from": SVSP[i], "sp": sp, "n": len(g), "n_frac": len(fr),
            "share": round(len(fr) / len(g), 2) if g else None, "n_clean_tail": tail,
            "ai_frac": _mean([r.get("ap_ai") for r in fr]), "ai_ok": _mean([r.get("ap_ai") for r in ok]),
            "status": status, "proposed": prop, "hold": hold,
            "last_frac_ts": max((r.get("ts") or "" for r in fr), default=None),
        })
    return {"rows": out, "step_um": step, "max_gap_um": cap, "min_gap_um": low, "min_zones": need,
            "clean_n": n_clean, "weak_pct": weak}
