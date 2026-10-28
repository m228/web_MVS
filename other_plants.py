"""other_plants — базы других заводов для сравнения на графиках отчёта (только просмотр, только у того, у кого есть папка).

Раскладка рядом с приложением:  database\\<завод>\\mvs.db  (название завода = название папки; рядом могут лежать -wal и -shm).
Чужая база открывается только на чтение (db.read_only_at) и не смешивается со своей: в аномалии, проверку модели и «Сравнение»
чужие варки не попадают. Нет папки database — функции возвращают пустой список, интерфейс ничего не показывает."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import cv_store
import db
import report
from paths import DATA_DIR

BASE = DATA_DIR / "database"
# поля варки, которые нужны графикам (остальное в чужой отчёт не тащим)
KEEP = ("start", "t_start", "t_end", "flag", "fines_avg", "fin_um", "fin_cv", "fin_agg_pct", "duration_min", "sv_end", "water_dose",
        "seed_ready_min", "seed_end_min", "model", "temp_avg", "vac_avg")


def names() -> list[str]:
    """Заводы, у которых в database\\<завод> лежит mvs.db."""
    try:
        return sorted(p.name for p in BASE.iterdir() if p.is_dir() and (p / "mvs.db").is_file())
    except OSError:
        return []


def _main_serial() -> Optional[str]:
    """Камера завода: серийник с наибольшим числом проб (в базе завода он один; если несколько — основной)."""
    r = db.conn().execute("SELECT serial, COUNT(*) n FROM journal GROUP BY serial ORDER BY n DESC LIMIT 1").fetchone()
    return r["serial"] if r else None


def one(name: str, t_from: float, t_to: float, fin_sv: float = report.FIN_SV) -> Optional[dict]:
    """Варки одного чужого завода за период: ряды для графиков + чем считалась мука (модель, порог «Мука с СВ»)."""
    path = BASE / name / "mvs.db"
    if not path.is_file():
        return None
    with db.read_only_at(path):
        serial = _main_serial()
        if not serial:
            return None
        rows = db.read_rows(serial)
        rows.sort(key=lambda r: r["t"])
        rows = cv_store._null_valve_garbage(cv_store._null_sv_outliers(rows))
        thr = next((r["fines_from_sv"] for r in reversed(rows) if r.get("fines_from_sv") is not None), None)
        cv_store._apply_fines_gate(rows, thr)               # порог муки — этого завода, а не локального
        groups = cv_store._group_boils(rows)
        rep = report.build(serial, t_from, t_to, fin_sv=fin_sv, groups=groups)
    models = sorted({b.get("model") for b in rep["boils"] if b.get("model")})
    return {"name": name, "serial": serial, "fines_from_sv": thr, "models": models,
            "boils": [{k: b.get(k) for k in KEEP} for b in rep["boils"]]}


def report_all(t_from: float, t_to: float, fin_sv: float = report.FIN_SV) -> list[dict]:
    out = []
    for n in names():
        try:
            r = one(n, t_from, t_to, fin_sv)
        except Exception as e:                          # битая или чужой версии база не должна ронять отчёт
            r = {"name": n, "error": str(e)[:200], "boils": []}
        if r:
            out.append(r)
    return out
