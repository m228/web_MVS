"""store — хранилище сервиса движения в одном файле SQLite (data\\motion.db): тренды, записи метрик по кадрам, снимки.

Таблицы (компактно: имена камер/зон/статусов — числами из names, время — целым, значения — целыми ×10000):
  trend    — точка раз в секунду на зону (cam, ts в 0,1 с, zone) → flow, diff, bit, ok          — «сырое», хранится keep_days
  stat     — корзина 10 с на зону (cam, key, zone) → n, Σflow, min, max, Σdiff, min, max        — навсегда (на порядок меньше сырого)
  rec_run  — один запуск записи камеры (t0 … t1; t1 пусто — идёт); rec — строка на кадр (cam, ts в мс) → alive, status;
  rec_zone — значения зон кадра                                                                 — «сырое», хранится keep_days
  shot     — сводка снимков «в один момент» (по строке на зону)                                 — навсегда (JPEG — файлами, чистятся отдельно)

Запись идёт пачками: add_* кладёт строки в память, flush() пишет их одной транзакцией (раз в FLUSH_S и по сбою). Соединение одно на
процесс, под замком — писать могут все камеры. CSV остаётся форматом ВЫГРУЗКИ (export_run_csv): Excel открывает как раньше.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import os
import threading
import time
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger("motion.store")

DB_NAME = "motion.db"
FLUSH_S = 5.0                          # буфер уходит на диск не реже раз в 5 с: при остановке теряется не больше этого хвоста
SCALE = 10000                          # значения хранятся целыми ×10000 (как и в CSV — 4 знака после точки)
_LIM = 1e11                            # защита: «бесконечные» заглушки (min = 1e30) не должны переполнить 64-битное целое
MAX_BUF = 300_000                      # строк в памяти при сбое диска: дальше отбрасываем самое старое, а не растём без предела
PURGE_CHUNK_S = 3600                   # уборка режется на куски по часу: замок хранилища на время куска, камеры не встают

_REG: dict = {}
_REG_LOCK = threading.Lock()


def _q(v) -> int:
    if v != v:                                          # NaN → 0, а не «бесконечность»
        return 0
    return int(round(max(-_LIM, min(_LIM, float(v))) * SCALE))


def _f(v) -> float:
    return v / SCALE


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def store_for(base) -> "MotionStore":
    """Хранилище каталога данных (одно на каталог на процесс)."""
    key = str(Path(base).resolve())
    with _REG_LOCK:
        s = _REG.get(key)
        if s is None:
            path = Path(base) / DB_NAME
            try:
                s = MotionStore(path)
            except sqlite3.DatabaseError as e:                  # битая база не должна валить сервис: откладываем файл в сторону и начинаем новую
                bad = path.with_name(path.name + ".corrupt")
                log.error("%s не открылась (%s) — откладываю в %s и начинаю новую", DB_NAME, e, bad.name)
                for suf in ("", "-wal", "-shm"):
                    try:
                        os.replace(str(path) + suf, str(bad) + suf)
                    except OSError:
                        pass
                s = MotionStore(path)
            _REG[key] = s
        return s


def close_for(base) -> None:
    with _REG_LOCK:
        s = _REG.pop(str(Path(base).resolve()), None)
    if s:
        s.close()


class MotionStore:
    def __init__(self, path: Path):
        self._c = None
        try:
            self._open(path)
        except sqlite3.Error:
            if self._c is not None:                            # недооткрытый файл держит замок (Windows): без этого его не отложить в сторону
                self._c.close()
            raise

    def _open(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        c = self._c = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        c.execute("PRAGMA auto_vacuum=INCREMENTAL")        # действует на новом файле: место после уборки старого возвращается
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS names (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS trend (cam INTEGER NOT NULL, ts INTEGER NOT NULL, zone INTEGER NOT NULL, flow INTEGER NOT NULL,
                diff INTEGER NOT NULL, bit INTEGER NOT NULL, ok INTEGER NOT NULL, PRIMARY KEY (cam, ts, zone)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS stat (cam INTEGER NOT NULL, key INTEGER NOT NULL, zone INTEGER NOT NULL, n INTEGER NOT NULL,
                sum_flow INTEGER NOT NULL, min_flow INTEGER NOT NULL, max_flow INTEGER NOT NULL,
                sum_diff INTEGER NOT NULL, min_diff INTEGER NOT NULL, max_diff INTEGER NOT NULL, PRIMARY KEY (cam, key, zone)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS rec_run (id INTEGER PRIMARY KEY AUTOINCREMENT, cam INTEGER NOT NULL, t0 REAL NOT NULL, t1 REAL,
                zones TEXT NOT NULL, fill TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_rec_run ON rec_run (cam, t0);
            CREATE TABLE IF NOT EXISTS rec (cam INTEGER NOT NULL, ts INTEGER NOT NULL, alive INTEGER, status INTEGER NOT NULL,
                PRIMARY KEY (cam, ts)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS rec_zone (cam INTEGER NOT NULL, ts INTEGER NOT NULL, zone INTEGER NOT NULL, flow INTEGER NOT NULL,
                diff INTEGER NOT NULL, bit INTEGER NOT NULL, tex REAL, lum REAL, rel REAL, fill INTEGER,
                PRIMARY KEY (cam, ts, zone)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS shot (ts INTEGER NOT NULL, cam INTEGER NOT NULL, zone INTEGER NOT NULL, connected INTEGER, alive INTEGER,
                status INTEGER, fps REAL, flow REAL, diff REAL, bit REAL, photo INTEGER, PRIMARY KEY (ts, cam, zone)) WITHOUT ROWID;
        """)
        self._names: dict = {name: i for i, name in c.execute("SELECT id, name FROM names")}
        self._by_id: dict = {i: n for n, i in self._names.items()}
        self._tr: list = []
        self._st: list = []
        self._rc: list = []
        self._rz: list = []
        self._rec_last: dict = {}
        self._flushed = 0.0
        self._closed = False
        # запуск записи, оставшийся «идущим» после остановки сервиса, закрываем на последней его строке
        for rid, cam, t0 in c.execute("SELECT id, cam, t0 FROM rec_run WHERE t1 IS NULL").fetchall():
            last = c.execute("SELECT MAX(ts) FROM rec WHERE cam=? AND ts>=?", (cam, int(t0 * 1000))).fetchone()[0]
            c.execute("UPDATE rec_run SET t1=? WHERE id=?", ((last / 1000.0) if last else t0, rid))
        c.commit()

    # ───────────── имена ─────────────
    def _id(self, name: str) -> int:
        i = self._names.get(name)
        if i is None:
            r = self._c.execute("SELECT id FROM names WHERE name=?", (name,)).fetchone()
            i = r[0] if r else self._c.execute("INSERT INTO names (name) VALUES (?)", (name,)).lastrowid
            self._names[name] = i
            self._by_id[i] = name
        return i

    def _name(self, i: int) -> str:
        n = self._by_id.get(i)
        if n is None:
            r = self._c.execute("SELECT name FROM names WHERE id=?", (i,)).fetchone()
            n = self._by_id[i] = r[0] if r else ""
        return n

    # ───────────── запись пачками ─────────────
    def maybe_flush(self, now: float = 0.0, force: bool = False) -> None:
        """Сбросить буфер на диск, если прошло FLUSH_S (по монотонным часам: перевод системного времени не ломает) или force.
        Метку времени ставим ДО записи — при сбое диска не долбимся в него на каждом кадре."""
        with self._lock:
            t = time.monotonic()
            if force or t - self._flushed >= FLUSH_S:
                self._flushed = t
                self.flush()

    def flush(self) -> None:
        with self._lock:
            if self._closed:
                return
            c = self._c
            try:
                if self._tr:
                    c.executemany("INSERT OR REPLACE INTO trend VALUES (?,?,?,?,?,?,?)", self._tr)
                if self._st:
                    c.executemany("INSERT OR REPLACE INTO stat VALUES (?,?,?,?,?,?,?,?,?,?)", self._st)
                if self._rc:
                    c.executemany("INSERT OR REPLACE INTO rec VALUES (?,?,?,?)", self._rc)
                if self._rz:
                    c.executemany("INSERT OR REPLACE INTO rec_zone VALUES (?,?,?,?,?,?,?,?,?,?)", self._rz)
                c.commit()
            except sqlite3.Error:
                try:
                    c.rollback()
                except sqlite3.Error:
                    pass
                for name in ("_tr", "_st", "_rc", "_rz"):          # диск не принимает — держим хвост в памяти, но не бесконечно
                    buf = getattr(self, name)
                    if len(buf) > MAX_BUF:
                        log.warning("буфер %s переполнен (диск не пишется): отбрасываю старые строки", name[1:])
                        del buf[:len(buf) - MAX_BUF]
                raise
            self._tr, self._st, self._rc, self._rz = [], [], [], []

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self.flush()
                self._c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as e:
                log.warning("хранилище при закрытии: %s", e)
            self._closed = True
            self._c.close()

    # ───────────── тренды ─────────────
    def add_trend(self, cam: str, ts: float, zones: dict, ok: int) -> None:
        """zones = {зона: (flow, diff, bit)}"""
        with self._lock:
            c = self._id(cam)
            self._tr += [(c, int(round(ts * 10)), self._id(z), _q(v[0]), _q(v[1]), int(v[2]), int(ok)) for z, v in zones.items()]

    def add_stat(self, cam: str, key: int, zones: dict) -> None:
        """zones = {зона: [n, Σflow, min, max, Σdiff, min, max]}"""
        with self._lock:
            c = self._id(cam)
            self._st += [(c, int(key), self._id(z), int(v[0])) + tuple(_q(x) for x in v[1:7]) for z, v in zones.items()]

    def load_trend(self, cam: str, now: float, long_s: float, stats_s: float, zone_ids: set):
        """-> (длинная история [(ts, {зона: (flow, diff, bit)}, ok)], корзины [[ключ, {зона: [n, Σflow, min, max, Σdiff, min, max]}]])
        за последние long_s / stats_s секунд; зоны, которых больше нет в настройках, пропускаются."""
        with self._lock:
            self.flush()
            c = self._id(cam)
            longs: dict = {}
            for ts, z, fl, df, bit, ok in self._c.execute(
                    "SELECT ts, zone, flow, diff, bit, ok FROM trend WHERE cam=? AND ts>=? AND ts<=? ORDER BY ts",
                    (c, int((now - long_s) * 10), int((now + 5) * 10))):
                zn = self._name(z)
                if zn in zone_ids:
                    zs, o = longs.setdefault(ts / 10.0, ({}, 1))
                    zs[zn] = (_f(fl), _f(df), bit)
                    longs[ts / 10.0] = (zs, min(o, ok))
            stats: dict = {}
            for key, z, n, sf, mnf, mxf, sd, mnd, mxd in self._c.execute(
                    "SELECT key, zone, n, sum_flow, min_flow, max_flow, sum_diff, min_diff, max_diff FROM stat "
                    "WHERE cam=? AND key>=? AND key<=? ORDER BY key", (c, int((now - stats_s) // 10), int((now + 5) // 10))):
                zn = self._name(z)
                if zn in zone_ids:
                    stats.setdefault(key, {})[zn] = [n] + [_f(x) for x in (sf, mnf, mxf, sd, mnd, mxd)]
        return [(ts, longs[ts][0], longs[ts][1]) for ts in sorted(longs)], [[k, stats[k]] for k in sorted(stats)]

    def visibility(self, cam: str, zone_ids: set, t0: float, t1: float, step_s: int) -> dict:
        """Сколько секунд из каждого окна step_s палка была найдена: {зона: {номер окна: (секунд с данными, секунд «найдена»)}}.
        Считаем только секунды, когда камера отдавала нормальную картинку (ok=1): обрыв связи не считается «палку не видно».
        Номер окна = время // step_s. Источник — секундные тренды (бит секунды = «найдена хотя бы в одном кадре секунды»)."""
        step = max(1, int(step_s))
        with self._lock:
            self.flush()
            c = self._id(cam)
            ids = {self._id(z): z for z in zone_ids}
            out: dict = {z: {} for z in zone_ids}
            for k, z, n, f in self._c.execute(
                    "SELECT ts / ? AS k, zone, COUNT(*), SUM(bit) FROM trend WHERE cam=? AND ts>=? AND ts<? AND ok=1 GROUP BY k, zone",
                    (step * 10, c, int(t0 * 10), int(t1 * 10))):
                if z in ids:
                    out[ids[z]][int(k)] = (int(n), int(f or 0))
        return out

    # ───────────── записи по кадрам ─────────────
    def rec_start(self, cam: str, zone_ids: list, fill_ids: set, t: float) -> int:
        with self._lock:
            c = self._id(cam)
            self.flush()
            self._c.execute("UPDATE rec_run SET t1=? WHERE cam=? AND t1 IS NULL", (t, c))
            rid = self._c.execute("INSERT INTO rec_run (cam, t0, zones, fill) VALUES (?,?,?,?)",
                                  (c, t, json.dumps(list(zone_ids)), json.dumps(sorted(fill_ids)))).lastrowid
            self._c.commit()
            return rid

    def rec_stop(self, run: int, t: float) -> None:
        with self._lock:
            if self._closed:
                return
            self.flush()
            self._c.execute("UPDATE rec_run SET t1=? WHERE id=? AND t1 IS NULL", (t, run))
            self._c.commit()

    def rec_add(self, cam: str, ts: float, alive: Optional[int], status: str, zones: dict) -> None:
        """Строка кадра. zones = {зона: {flow, diff, bit[, tex, lum, rel, fill]}} (у события пусто)."""
        with self._lock:
            c = self._id(cam)
            ms = max(int(round(ts * 1000)), self._rec_last.get(c, 0) + 1)     # одна камера — одно время на строку
            self._rec_last[c] = ms
            self._rc.append((c, ms, alive, self._id(status)))
            for zid, z in zones.items():
                self._rz.append((c, ms, self._id(zid), _q(z.get("flow", 0)), _q(z.get("diff", 0)), int(z.get("bit", 0)),
                                 z.get("tex"), z.get("lum"), z.get("rel"), None if z.get("fill") is None else int(z["fill"])))

    def runs(self, since: float = 0.0) -> list:
        """Запуски записи, которые шли после since: [{id, cam, t0, t1, zones, fill}], по камере и времени."""
        with self._lock:
            self.flush()
            out = []
            for rid, c, t0, t1, zs, fl in self._c.execute(
                    "SELECT id, cam, t0, t1, zones, fill FROM rec_run WHERE COALESCE(t1, ?) >= ? ORDER BY cam, t0", (time.time(), since)):
                out.append({"id": rid, "cam": self._name(c), "t0": t0, "t1": t1, "zones": json.loads(zs), "fill": set(json.loads(fl))})
            return out

    def _span(self, run: dict, since: float = 0.0) -> tuple:
        lo = int(max(run["t0"], since) * 1000)
        hi = int(round((run["t1"] if run["t1"] is not None else time.time() + 3600) * 1000)) + 1
        return self._id(run["cam"]), lo, hi

    def run_rows(self, run: dict, since: float = 0.0) -> Iterator[tuple]:
        """(ts, alive, status) по порядку; alive у события None. Потоком — запись за недели в память не грузим."""
        with self._lock:
            c, lo, hi = self._span(run, since)
        cur = sqlite3.connect(str(self.path), timeout=30)
        try:
            for ms, alive, st in cur.execute("SELECT r.ts, r.alive, n.name FROM rec r JOIN names n ON n.id=r.status "
                                             "WHERE r.cam=? AND r.ts>=? AND r.ts<? ORDER BY r.ts", (c, lo, hi)):
                yield ms / 1000.0, alive, st
        finally:
            cur.close()

    def run_zone_stats(self, run: dict, since: float = 0.0) -> dict:
        """{зона: {"n", "flow": (ср, мин, макс), "diff": (ср, мин, макс), "bit": доля}} по кадрам, где камера жива и status = ok.
        Считает на своём соединении без замка хранилища: запрос по неделе записи камеры не останавливает."""
        with self._lock:
            self.flush()
            c, lo, hi = self._span(run, since)
            ok = self._id("ok")
            self._c.commit()
        out = {}
        db = sqlite3.connect(str(self.path), timeout=30)
        try:
            rows = db.execute(
                "SELECT z.zone, COUNT(*), AVG(z.flow), MIN(z.flow), MAX(z.flow), AVG(z.diff), MIN(z.diff), MAX(z.diff), AVG(z.bit) "
                "FROM rec_zone z JOIN rec r ON r.cam=z.cam AND r.ts=z.ts WHERE z.cam=? AND z.ts>=? AND z.ts<? AND r.alive=1 AND r.status=? "
                "GROUP BY z.zone", (c, lo, hi, ok)).fetchall()
        finally:
            db.close()
        with self._lock:
            for z, n, af, mnf, mxf, ad, mnd, mxd, ab in rows:
                out[self._name(z)] = {"n": n, "flow": (_f(af), _f(mnf), _f(mxf)), "diff": (_f(ad), _f(mnd), _f(mxd)), "bit": ab}
        order = {z: i for i, z in enumerate(run["zones"])}
        return dict(sorted(out.items(), key=lambda kv: order.get(kv[0], 99)))

    def write_run_csv(self, run: dict, out, since: float = 0.0) -> int:
        """CSV запуска в прежнем виде: time;<зона>_flow;_diff;_bit[;_tex;_lum;_rel;_fill];alive;status — «;». Пишет строками в out
        (текстовый поток) — запись за недели в память целиком не собирается. -> сколько строк."""
        with self._lock:
            self.flush()
            c, lo, hi = self._span(run, since)
            ids = run["zones"]
            zid = {z: self._id(z) for z in ids}
            self._c.commit()
        w = csv.writer(out, delimiter=";", lineterminator="\r\n")
        head = ["time"]
        for z in ids:
            head += ["%s_flow" % z, "%s_diff" % z, "%s_bit" % z]
            if z in run["fill"]:
                head += ["%s_tex" % z, "%s_lum" % z, "%s_rel" % z, "%s_fill" % z]
        w.writerow(head + ["alive", "status"])
        n = 0
        db = sqlite3.connect(str(self.path), timeout=30)
        try:
            zrows = db.execute("SELECT ts, zone, flow, diff, bit, tex, lum, rel, fill FROM rec_zone WHERE cam=? AND ts>=? AND ts<? ORDER BY ts",
                               (c, lo, hi))
            pend = zrows.fetchone()
            for ms, alive, st in db.execute("SELECT r.ts, r.alive, n.name FROM rec r JOIN names n ON n.id=r.status "
                                            "WHERE r.cam=? AND r.ts>=? AND r.ts<? ORDER BY r.ts", (c, lo, hi)):
                byz = {}
                while pend is not None and pend[0] <= ms:
                    if pend[0] == ms:
                        byz[pend[1]] = pend
                    pend = zrows.fetchone()
                row = [_stamp(ms / 1000.0) + ".%03d" % (ms % 1000)]
                for z in ids:
                    p = byz.get(zid[z])
                    row += ["", "", ""] if p is None else [_f(p[2]), _f(p[3]), p[4]]
                    if z in run["fill"]:
                        row += ["", "", "", ""] if p is None else ["" if v is None else v for v in (p[5], p[6], p[7], p[8])]
                w.writerow(row + ["" if alive is None else alive, st])
                n += 1
        finally:
            db.close()
        return n

    def export_run_csv(self, run: dict, since: float = 0.0) -> bytes:
        """То же одним куском байт (UTF-8 с BOM, Excel открывает) — для малых запусков и тестов; архив пишет write_run_csv потоком."""
        buf = io.StringIO(newline="")
        self.write_run_csv(run, buf, since)
        return buf.getvalue().encode("utf-8-sig")

    # ───────────── снимки ─────────────
    def add_shot(self, ts: float, cam: str, zone: str, connected: bool, alive: bool, status: str, fps, flow, diff, bit, photo: bool) -> None:
        with self._lock:
            self._c.execute("INSERT OR REPLACE INTO shot VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                int(ts), self._id(cam), self._id(zone or ""), int(connected), int(alive), self._id(status or ""),
                None if fps in ("", None) else float(fps), None if flow in ("", None) else float(flow),
                None if diff in ("", None) else float(diff), None if bit in ("", None) else float(bit), int(photo)))
            self._c.commit()

    def shots(self, t_from: float, t_to: float) -> list:
        with self._lock:
            return [{"ts": ts, "cam": self._name(c), "zone": self._name(z), "connected": cn, "alive": al, "status": self._name(st), "photo": ph}
                    for ts, c, z, cn, al, st, ph in self._c.execute(
                        "SELECT ts, cam, zone, connected, alive, status, photo FROM shot WHERE ts>=? AND ts<? ORDER BY ts, cam, zone",
                        (int(t_from), int(t_to)))]

    # ───────────── уборка ─────────────
    def purge_raw(self, edge: float, vacuum_pages: int = 5000) -> int:
        """Удалить сырое (секундные тренды, кадры записи, закончившиеся запуски) старше edge. Корзины и снимки не трогаем.
        Режет по часу: замок хранилища берётся на каждый кусок, между кусками камеры пишут. vacuum_pages — сколько страниц вернуть диску."""
        with self._lock:
            self.flush()
            cams = [r[0] for r in self._c.execute("SELECT id FROM names").fetchall()]
        n = 0
        for c in cams:
            for table, unit in (("trend", 10), ("rec", 1000), ("rec_zone", 1000)):
                cut = int(edge * unit)
                step = PURGE_CHUNK_S * unit
                while True:
                    with self._lock:
                        lo = self._c.execute("SELECT MIN(ts) FROM %s WHERE cam=?" % table, (c,)).fetchone()[0]
                        if lo is None or lo >= cut:
                            break
                        done = self._c.execute("DELETE FROM %s WHERE cam=? AND ts<?" % table, (c, min(cut, lo + step))).rowcount
                        self._c.commit()
                        if table != "rec_zone":
                            n += done
        with self._lock:
            self._c.execute("DELETE FROM rec_run WHERE t1 IS NOT NULL AND t1<?", (edge,))
            self._c.commit()
            if n and vacuum_pages:
                self._c.execute("PRAGMA incremental_vacuum(%d)" % int(vacuum_pages)).fetchall()      # пока не выберем строки — страницы не освобождаются
                self._c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        return n

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0
