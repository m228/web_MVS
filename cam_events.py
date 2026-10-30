"""Хранилище событий камер — «эпизоды»: интервал от t0 до t1 (t1 пока пусто — эпизод идёт).

Что пишем (kind):
  link_lost   — RTSP-камера переподключается (нет кадров)
  gray        — серые/неполные кадры (декодер «стёр» картинку) — по выборке раз в секунду в rtsp_worker
  cv_dead     — КЗ без данных: нет связи с камерой (zone пусто, info — причина)
  cv_frozen   — КЗ: картинка застыла
  cv_corrupt  — КЗ: битый поток («каша»)
  zone_on     — зона «крутится»   (zone — id зоны, info — её имя)
  zone_off    — зона «стоит»

Из эпизодов собираются и журнал камер, и отчёт по смене. Пишет фоновый монитор (cam_monitor.py) — работает без открытой страницы.
Файл cam_events.db лежит в каталоге данных (переживает обновление), хранится RETENTION_DAYS дней.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from logger import log_event
from paths import DATA_DIR

DB_PATH = DATA_DIR / "cam_events.db"
RETENTION_DAYS = 30
BEAT_EVERY_S = 10.0        # как часто «пульс» (последний момент, когда монитор точно работал) пишем на диск

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_conn_path = None
_open: dict = {}           # (камера, kind, zone) -> id идущего эпизода
_beat = 0.0
_beat_saved = 0.0


def _db() -> sqlite3.Connection:
    global _conn, _conn_path
    if _conn is None or _conn_path != DB_PATH:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("CREATE TABLE IF NOT EXISTS episodes (id INTEGER PRIMARY KEY AUTOINCREMENT, cam TEXT NOT NULL, "
                  "kind TEXT NOT NULL, zone TEXT NOT NULL DEFAULT '', info TEXT NOT NULL DEFAULT '', t0 REAL NOT NULL, t1 REAL)")
        if "shot" not in {r["name"] for r in c.execute("PRAGMA table_info(episodes)")}:     # кадр события (путь внутри event_shots)
            c.execute("ALTER TABLE episodes ADD COLUMN shot TEXT NOT NULL DEFAULT ''")
        c.execute("CREATE INDEX IF NOT EXISTS ix_ep_cam ON episodes (cam, t0)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_ep_t1 ON episodes (t1)")
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        c.commit()
        _conn, _conn_path = c, DB_PATH
        _open.clear()
    return _conn


def recover() -> int:
    """Старт приложения: эпизоды, оставшиеся «идущими» с прошлого запуска, закрываем на последнем «пульсе» монитора
    (а не на сегодняшнем моменте — иначе простой приложения записался бы как длинный сбой камеры)."""
    with _lock:
        c = _db()
        row = c.execute("SELECT value FROM meta WHERE key='beat'").fetchone()
        beat = float(row["value"]) if row else None
        _open.clear()
        if beat is None:
            n = c.execute("UPDATE episodes SET t1=t0 WHERE t1 IS NULL").rowcount
        else:
            n = c.execute("UPDATE episodes SET t1=MAX(t0, ?) WHERE t1 IS NULL", (beat,)).rowcount
        c.commit()
        return n


def heartbeat(now: float) -> None:
    global _beat, _beat_saved
    with _lock:
        _beat = now
        if now - _beat_saved >= BEAT_EVERY_S:
            c = _db()
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('beat', ?)", (repr(now),))
            c.commit()
            _beat_saved = now


def sync(cam: str, scope: Iterable[str], active: dict, now: float) -> None:
    """Привести эпизоды камеры к текущему состоянию. scope — какие kind ведёт вызывающий (чужие не трогаем);
    active — {(kind, zone): info} того, что идёт сейчас. Пропавшее закрываем в now, новое открываем."""
    scope = set(scope)
    with _lock:
        c = _db()
        changed = False
        for key in [k for k in _open if k[0] == cam and k[1] in scope and (k[1], k[2]) not in active]:
            c.execute("UPDATE episodes SET t1=? WHERE id=?", (now, _open.pop(key)))
            changed = True
        for (kind, zone), info in active.items():
            if kind not in scope or (cam, kind, zone) in _open:
                continue
            cur = c.execute("INSERT INTO episodes (cam, kind, zone, info, t0) VALUES (?,?,?,?,?)",
                            (cam, kind, zone or "", info or "", now))
            _open[(cam, kind, zone)] = cur.lastrowid
            changed = True
        if changed:
            c.commit()


def purge(now: float) -> int:
    """Удалить завершённые эпизоды старше RETENTION_DAYS."""
    with _lock:
        c = _db()
        n = c.execute("DELETE FROM episodes WHERE t1 IS NOT NULL AND t1 < ?", (now - RETENTION_DAYS * 86400,)).rowcount
        c.commit()
    n_shots = purge_shots(now)
    if n or n_shots:
        log_event("cam_events", "Старые события камер удалены", "info", {"count": n, "shot_days": n_shots, "days": RETENTION_DAYS})
    return n


def episodes(cam: Optional[str] = None, t_from: Optional[float] = None, t_to: Optional[float] = None,
             kinds: Optional[Iterable[str]] = None, min_s: float = 0.0, limit: int = 500,
             now: Optional[float] = None) -> list:
    """Эпизоды, пересекающиеся с окном [t_from, t_to], новые сверху. Идущий эпизод считается до now (t1=None, dur растёт)."""
    now = time.time() if now is None else now
    where, args = ["COALESCE(t1, ?) - t0 >= ?"], [now, float(min_s)]
    if t_from is not None:
        where.append("COALESCE(t1, ?) >= ?")
        args += [now, t_from]
    if t_to is not None:
        where.append("t0 <= ?")
        args.append(t_to)
    if cam:
        where.append("cam = ?")
        args.append(cam)
    kinds = list(kinds) if kinds else []
    if kinds:
        where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
        args += kinds
    sql = "SELECT * FROM episodes WHERE %s ORDER BY t0 DESC, id DESC" % " AND ".join(where)
    if limit and int(limit) > 0:                # limit=0 — без ограничения (для отчёта нужны все эпизоды окна)
        sql += " LIMIT ?"
        args.append(min(int(limit), 5000))
    with _lock:
        rows = _db().execute(sql, args).fetchall()
    out = []
    for r in rows:
        t1 = r["t1"]
        out.append({"id": r["id"], "cam": r["cam"], "kind": r["kind"], "zone": r["zone"], "info": r["info"],
                    "t0": r["t0"], "t1": t1, "dur": round((t1 if t1 is not None else now) - r["t0"], 1),
                    "shot": bool(r["shot"])})
    return out


# ---------- кадры событий (снимок в момент остановки зоны / зависания) ----------
def shots_dir() -> Path:
    return DB_PATH.parent / "event_shots"


def open_id(cam: str, kind: str, zone: str = "") -> Optional[int]:
    """id идущего эпизода (чтобы привязать к нему снимок)."""
    with _lock:
        _db()
        return _open.get((cam, kind, zone))


def set_shot(ep_id: int, rel: str) -> None:
    with _lock:
        c = _db()
        c.execute("UPDATE episodes SET shot=? WHERE id=?", (rel, ep_id))
        c.commit()


def shot_file(ep_id: int) -> Optional[Path]:
    """Файл кадра эпизода или None. Путь строго внутри event_shots: из базы берём только относительный путь."""
    with _lock:
        row = _db().execute("SELECT shot FROM episodes WHERE id=?", (ep_id,)).fetchone()
    if not row or not row["shot"]:
        return None
    base = shots_dir().resolve()
    p = (base / row["shot"]).resolve()
    return p if base in p.parents and p.is_file() else None


_DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def purge_shots(now: float) -> int:
    """Удалить папки кадров старше RETENTION_DAYS. Трогаем только папки event_shots/<камера>/<ГГГГ-ММ-ДД> со строгим именем-датой."""
    base = shots_dir()
    if not base.is_dir():
        return 0
    cutoff = datetime.fromtimestamp(now - RETENTION_DAYS * 86400).strftime("%Y-%m-%d")
    n = 0
    for cam_dir in (p for p in base.iterdir() if p.is_dir()):
        for day in (p for p in cam_dir.iterdir() if p.is_dir() and _DAY_DIR.match(p.name)):
            if day.name < cutoff:
                shutil.rmtree(day, ignore_errors=True)
                n += 1
    return n
