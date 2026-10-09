"""db — SQLite-хранилище данных CV (журнал проб, варки, правки разломов).

Один файл на машину: <DATA_DIR>/mvs.db (переживает обновление, см. paths.py). Кадры PNG/JPG и объекты кристаллов в базу
не идут — остаются файлами в cv_results/ (у каждой пробы там result.json с полными данными).

Журнал проб (`journal`) — строка на пробу, ключ (serial, ts). Все числовые поля, по которым строятся тренды и отчёты, —
типизированные столбцы с индексами; полная исходная строка (как раньше в jsonl) — в `row_json`, так что ничего не теряется
и поля, которых ещё нет в схеме, не пропадают. Два вычисляемых при записи столбца:
  boil_id     — номер варки (по правилам is_new_boil: пауза / время варки упало / стадия откатилась / СВ упало);
  sv_max_run  — максимум СВ с начала этой варки: порог «Мука с СВ» применяется запросом (sv_max_run >= порог), а не циклом.

Потоки: соединение на поток (WAL), запись под общим замком — приложение пишет пробы из одного потока, UI читает из других.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from paths import DATA_DIR

DB_PATH = DATA_DIR / "mvs.db"
SCHEMA_VERSION = 1

# --- граница варки (единственное место правил; cv_store и тренд берут boil_id отсюда) ---
BOIL_GAP_S = 45 * 60        # пауза между пробами больше этой — новая варка (пробы идут раз в 1–2 мин)
BOIL_COOK_DROP_S = 300      # время варки (cook_time) упало больше чем на это — новая варка
BOIL_SV_DROP = 4.0          # СВ упало на столько и больше — новая варка (запасной признак)


def is_new_boil(prev: dict, r: dict) -> bool:
    """Начало новой варки между двумя соседними пробами журнала: длинная пауза, время варки упало,
    стадия откатилась на заводку (было ≥6, стало ≤4) или СВ резко упало."""
    if r["t"] - prev["t"] > BOIL_GAP_S:
        return True
    ct, pt = r.get("cook_time"), prev.get("cook_time")
    if ct is not None and pt is not None and ct < pt - BOIL_COOK_DROP_S:
        return True
    st, ps = r.get("stage"), prev.get("stage")
    if st is not None and ps is not None and ps >= 6 and st <= 4:
        return True
    # СВ упало — запасной признак, и только когда времени варки нет вовсе: одиночный сбой чтения СВ не должен делить варку
    sv, psv = r.get("sv"), prev.get("sv")
    return (ct is None or pt is None) and sv is not None and psv is not None and sv < psv - BOIL_SV_DROP


def null_sv_outliers(rows: list[dict]) -> list[dict]:
    """СВ, которое явно не настоящее (< 5 или сильно отличается от соседних проб — разовый сбой чтения ПЛК: 0,0 или 58 при 87),
    заменяем на None: иначе тренд падает в ноль, а варка делится на две. Сами данные не меняем."""
    for i, r in enumerate(rows):
        sv = r.get("sv")
        if sv is None:
            continue
        nb = sorted(x["sv"] for x in rows[max(0, i - 3):i] + rows[i + 1:i + 4] if x.get("sv") is not None)
        med = nb[len(nb) // 2] if len(nb) >= 2 else None
        if sv < 5 or (med is not None and abs(sv - med) > 15):
            r["sv"] = None
    return rows


# --- столбцы журнала: ключи строки (cv_store._hist_row) → типизированные столбцы ---
_INT_COLS = ("stage", "substage", "frames", "frac_zones", "cook_time", "seed_age", "good_n", "rej_n")
_SIEVE = ["sieve_%s_b%d" % (m, i) for m in ("m1", "m2", "m3", "area") for i in range(7)]
ROW_COLS = (
    ["t", "stage", "substage", "phase", "sv", "count", "mean", "median", "small", "medium", "large", "reject", "reject_pct",
     "cv_pct", "density", "suspect", "n_needle", "n_aggregate", "n_crooked", "n_tiny", "n_huge",
     "temp", "level", "current", "vac", "cook_time", "seed_age",
     "fines_m1", "fines_m2", "fines_m3", "fines_area", "fines_n", "agg_m1", "agg_m2", "agg_m3", "agg_area", "agg_n",
     "vtot_m1", "vtot_m2", "vtot_m3", "good_n", "rej_n", "fines_um", "fines_side_mm", "k_thick", "fines_from_sv",
     "frac_zones", "frac_pct", "frames"] + _SIEVE
)


def _col_type(c: str) -> str:
    return "INTEGER" if c in _INT_COLS else ("TEXT" if c == "phase" else "REAL")


_local = threading.local()
_wlock = threading.RLock()


def set_path(path) -> None:
    """Другая база (тесты, сверка на копии данных). Закрывает соединения этого потока."""
    global DB_PATH
    close()
    DB_PATH = Path(path)


def close() -> None:
    conns = getattr(_local, "conns", None) or {}
    for c in conns.values():
        try:
            c.close()
        except Exception:
            pass
    _local.conns = {}


def conn() -> sqlite3.Connection:
    """Соединение этого потока с текущей базой (создаёт файл и схему при первом обращении)."""
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    key = str(DB_PATH)
    c = conns.get(key)
    if c is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(key, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        conns[key] = c
        _init(c)
    return c


def _init(c: sqlite3.Connection) -> None:
    with _wlock:
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        cols = ", ".join("%s %s" % (k, _col_type(k)) for k in ROW_COLS)
        c.execute("CREATE TABLE IF NOT EXISTS journal ("
                  "serial TEXT NOT NULL, ts TEXT NOT NULL, boil_id INTEGER, sv_max_run REAL, %s, row_json TEXT NOT NULL, "
                  "PRIMARY KEY (serial, ts))" % cols)
        have = {r["name"] for r in c.execute("PRAGMA table_info(journal)")}
        for k in ROW_COLS:                                    # схема выросла (новое поле) — дописать столбец
            if k not in have:
                c.execute("ALTER TABLE journal ADD COLUMN %s %s" % (k, _col_type(k)))
        c.execute("CREATE INDEX IF NOT EXISTS ix_journal_t ON journal (serial, t)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_journal_boil ON journal (serial, boil_id)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_journal_sub ON journal (serial, substage)")
        # журнал правок разломов оператором (кто/что/когда): для разбора и обучения
        c.execute("CREATE TABLE IF NOT EXISTS fracture_edits (id INTEGER PRIMARY KEY AUTOINCREMENT, serial TEXT NOT NULL, "
                  "ts TEXT NOT NULL, at REAL NOT NULL, action TEXT NOT NULL, zid TEXT, frame_idx INTEGER)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_fedit ON fracture_edits (serial, ts)")
        c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        c.commit()


def meta_get(key: str, default=None):
    r = conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def meta_set(key: str, value) -> None:
    with _wlock:
        c = conn()
        c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        c.commit()


# --- журнал проб ---
def _vals(row: dict) -> list:
    return [row.get(k) for k in ROW_COLS]


def upsert_row(serial: str, row: dict) -> None:
    """Записать/обновить строку журнала. Новая проба в конце — boil_id и sv_max_run считаются по предыдущей; проба «в прошлое»
    (дописали старую) — варки пересчитываются целиком. Обновление существующей строки сохраняет её boil_id."""
    if row.get("ts") is None or row.get("t") is None:
        return
    with _wlock:
        c = conn()
        old = c.execute("SELECT boil_id, sv_max_run FROM journal WHERE serial=? AND ts=?", (serial, row["ts"])).fetchone()
        later = c.execute("SELECT 1 FROM journal WHERE serial=? AND t>? LIMIT 1", (serial, row["t"])).fetchone()
        boil_id, sv_run = (old["boil_id"], old["sv_max_run"]) if old else (None, None)
        if not old:
            prev = c.execute("SELECT t, cook_time, stage, sv, boil_id, sv_max_run FROM journal WHERE serial=? AND t<=? AND ts<>? "
                             "ORDER BY t DESC LIMIT 1", (serial, row["t"], row["ts"])).fetchone()
            if prev is not None and prev["boil_id"] is not None and not is_new_boil(dict(prev), row):
                boil_id = prev["boil_id"]
                sv_run = max([x for x in (prev["sv_max_run"], row.get("sv")) if x is not None], default=None)
            else:
                nxt = c.execute("SELECT COALESCE(MAX(boil_id), 0) + 1 FROM journal WHERE serial=?", (serial,)).fetchone()[0]
                boil_id, sv_run = nxt, row.get("sv")
        c.execute("INSERT OR REPLACE INTO journal (serial, ts, boil_id, sv_max_run, %s, row_json) VALUES (?, ?, ?, ?, %s, ?)" %
                  (", ".join(ROW_COLS), ", ".join("?" * len(ROW_COLS))),
                  [serial, row["ts"], boil_id, sv_run] + _vals(row) + [json.dumps(row, ensure_ascii=False)])
        if later and not old:
            rebuild_boils(serial, commit=False)
        c.commit()


def upsert_many(serial: str, rows: list[dict]) -> int:
    """Пачка строк (миграция): одной транзакцией, варки пересчитываются один раз в конце. Возвращает число записанных."""
    n = 0
    with _wlock:
        c = conn()
        for row in rows:
            if row.get("ts") is None or row.get("t") is None:
                continue
            c.execute("INSERT OR REPLACE INTO journal (serial, ts, boil_id, sv_max_run, %s, row_json) VALUES (?, ?, NULL, NULL, %s, ?)" %
                      (", ".join(ROW_COLS), ", ".join("?" * len(ROW_COLS))),
                      [serial, row["ts"]] + _vals(row) + [json.dumps(row, ensure_ascii=False)])
            n += 1
        rebuild_boils(serial, commit=False)
        c.commit()
    return n


def rebuild_boils(serial: str, commit: bool = True) -> int:
    """Пересчитать boil_id и sv_max_run по всему журналу серийника (с учётом сбойных СВ, как при показе). Возвращает число варок."""
    with _wlock:
        c = conn()
        rows = [dict(r) for r in c.execute("SELECT ts, t, cook_time, stage, sv FROM journal WHERE serial=? ORDER BY t, ts", (serial,))]
        null_sv_outliers(rows)
        bid, prev, run, upd = 0, None, None, []
        for r in rows:
            if prev is None or is_new_boil(prev, r):
                bid, run = bid + 1, None
            if r.get("sv") is not None:
                run = r["sv"] if run is None else max(run, r["sv"])
            upd.append((bid, run, serial, r["ts"]))
            prev = r
        c.executemany("UPDATE journal SET boil_id=?, sv_max_run=? WHERE serial=? AND ts=?", upd)
        if commit:
            c.commit()
        return bid


def read_rows(serial: str, t_from: Optional[float] = None, t_to: Optional[float] = None, day: Optional[str] = None,
              with_boil: bool = False) -> list[dict]:
    """Строки журнала по времени — те же словари, что раньше в jsonl (из row_json). with_boil добавляет boil_id и sv_max_run."""
    sql, args = "SELECT row_json, boil_id, sv_max_run FROM journal WHERE serial=?", [serial]
    if day:
        sql += " AND ts LIKE ?"
        args.append(day + "%")
    if t_from is not None:
        sql += " AND t>=?"
        args.append(t_from)
    if t_to is not None:
        sql += " AND t<=?"
        args.append(t_to)
    out = []
    for r in conn().execute(sql + " ORDER BY t, ts", args):
        d = json.loads(r["row_json"])
        if with_boil:
            d["boil_id"], d["sv_max_run"] = r["boil_id"], r["sv_max_run"]
        out.append(d)
    return out


def days(serial: str) -> list[str]:
    """Даты (YYYY-MM-DD), за которые есть пробы, по возрастанию."""
    return [r[0] for r in conn().execute("SELECT DISTINCT substr(ts, 1, 10) FROM journal WHERE serial=? ORDER BY 1", (serial,))]


def count(serial: Optional[str] = None) -> int:
    if serial is None:
        return conn().execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    return conn().execute("SELECT COUNT(*) FROM journal WHERE serial=?", (serial,)).fetchone()[0]


def known_ts(serial: str) -> set:
    return {r[0] for r in conn().execute("SELECT ts FROM journal WHERE serial=?", (serial,))}


def serials() -> list[str]:
    return [r[0] for r in conn().execute("SELECT DISTINCT serial FROM journal ORDER BY 1")]


def patch_row(serial: str, ts: str, fields: dict) -> bool:
    """Поправить поля одной строки (правка разломов, пересчёт), остальные не трогаем. False — такой пробы нет."""
    with _wlock:
        c = conn()
        r = c.execute("SELECT row_json FROM journal WHERE serial=? AND ts=?", (serial, ts)).fetchone()
        if r is None:
            return False
        d = json.loads(r["row_json"])
        d.update(fields)
        sets = ["row_json=?"] + ["%s=?" % k for k in fields if k in ROW_COLS]
        args = [json.dumps(d, ensure_ascii=False)] + [fields[k] for k in fields if k in ROW_COLS] + [serial, ts]
        c.execute("UPDATE journal SET %s WHERE serial=? AND ts=?" % ", ".join(sets), args)
        c.commit()
        return True


def log_fracture_edit(serial: str, ts: str, action: str, zid: Optional[str], frame_idx: int, at: float) -> None:
    with _wlock:
        c = conn()
        c.execute("INSERT INTO fracture_edits (serial, ts, at, action, zid, frame_idx) VALUES (?, ?, ?, ?, ?, ?)",
                  (serial, ts, at, action, zid, int(frame_idx)))
        c.commit()
