"""trends — тренды и снимки «в один момент» на диске (data\\trends, data\\shots), чтобы они переживали перезапуск и обновление.

Тренды: каждая камера пишет два компактных CSV по суткам (разделитель «;», открывается в Excel):
  trends\\<камера>_<ГГГГММДД>.csv        — точка раз в секунду на зону: time;ts;zone;flow;diff;bit;ok  (длинная история, 2 ч графика)
  trends\\<камера>_<ГГГГММДД>_stats.csv  — корзина 10 с на зону: time;key;zone;n;sum_flow;min_flow;max_flow;sum_diff;min_diff;max_diff
                                          (среднее / мин / макс за сутки: сумма и счётчик, чтобы среднее считалось точно)
При старте камеры эти файлы читаются обратно в память — графики и «среднее / мин / макс» не обнуляются после обновления.

Снимки: раз в N минут (по умолчанию 20, по часам: :00 :20 :40) все камеры снимаются одновременно:
  shots\\<ГГГГММДД>\\<ЧЧММСС>\\<камера>.jpg + log.json (значения зон, связь, статус) ; shots\\<ГГГГММДД>\\log.csv — сводка за сутки.
На кадре нарисованы зоны (зелёная — «движется», красная — «стоит») и текущие diff/flow.
Всё старше KEEP_DAYS суток удаляется (purge).
"""
from __future__ import annotations

import csv
import logging
import shutil
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("motion.trends")

KEEP_DAYS = 10
FLUSH_S = 5.0                          # буфер уходит на диск не реже раз в 5 с: при остановке теряется не больше этого хвоста
LONG_HEAD = ["time", "ts", "zone", "flow", "diff", "bit", "ok"]
STATS_HEAD = ["time", "key", "zone", "n", "sum_flow", "min_flow", "max_flow", "sum_diff", "min_diff", "max_diff"]


def _day(ts: float) -> str:
    return time.strftime("%Y%m%d", time.localtime(ts))


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


class _DayFile:
    """Дописываемый CSV за сутки: сменился день — открываем следующий файл."""

    def __init__(self, path_for_day, head: list):
        self._path_for_day = path_for_day
        self._head = head
        self._day = ""
        self._fh = None
        self._csv = None

    def write(self, ts: float, rows: list):
        day = _day(ts)
        if day != self._day or self._fh is None:
            self.close()
            p = self._path_for_day(day)
            p.parent.mkdir(parents=True, exist_ok=True)
            new = not p.is_file() or p.stat().st_size == 0
            self._fh = open(p, "a", newline="", encoding="utf-8-sig")
            self._csv = csv.writer(self._fh, delimiter=";")
            if new:
                self._csv.writerow(self._head)
            self._day = day
        self._csv.writerows(rows)

    def flush(self):
        if self._fh:
            self._fh.flush()

    def close(self):
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass
        self._fh = self._csv = None
        self._day = ""


class TrendLog:
    """Тренды одной камеры: запись (add_*) и чтение при старте (load). Звать под замком камеры; ошибки диска камеру не ломают."""

    def __init__(self, base: Path, cam_id: str):
        self.dir = Path(base) / "trends"
        self.cam = cam_id
        self._long = _DayFile(lambda d: self.dir / ("%s_%s.csv" % (cam_id, d)), LONG_HEAD)
        self._stats = _DayFile(lambda d: self.dir / ("%s_%s_stats.csv" % (cam_id, d)), STATS_HEAD)
        self._flushed = 0.0
        self._closed = False
        self._warned = False

    def _guard(self, fn):
        if self._closed:
            return
        try:
            fn()
        except OSError as e:
            if not self._warned:
                self._warned = True
                log.warning("[%s] тренды на диск не пишутся: %s", self.cam, e)

    def _maybe_flush(self, ts: float):
        if ts - self._flushed >= FLUSH_S:
            self._flushed = ts
            self._long.flush()
            self._stats.flush()

    def add_long(self, ts: float, zones: dict, ok: int):
        """Секундная точка: zones = {зона: (flow, diff, bit)}."""
        def go():
            t = _stamp(ts)
            self._long.write(ts, [[t, "%.1f" % ts, zid, round(v[0], 4), round(v[1], 4), int(v[2]), int(ok)] for zid, v in zones.items()])
            self._maybe_flush(ts)
        self._guard(go)

    def add_stats(self, bucket: list):
        """Корзина статистики: [ключ 10-секундки, {зона: [n, Σflow, min, max, Σdiff, min, max]}]."""
        def go():
            ts = bucket[0] * 10.0
            self._stats.write(ts, [[_stamp(ts), bucket[0], zid, v[0]] + [round(x, 5) for x in v[1:]] for zid, v in bucket[1].items()])
            self._maybe_flush(ts)
        self._guard(go)

    def close(self):
        self._closed = True
        self._long.close()
        self._stats.close()

    # ── чтение при старте ──
    def load(self, now: float, long_s: float, stats_s: float, zone_ids: set):
        """-> (длинная история [(ts, {зона: (flow, diff, bit)}, ok)], корзины статистики [[ключ, {зона: [..7..]}]]) за последние
        long_s / stats_s секунд; зоны, которых больше нет в настройках, пропускаются. Битые и оборванные строки пропускаются."""
        longs: dict = {}
        stats: dict = {}
        lo_l, lo_s = now - long_s, now - stats_s
        start = now - max(long_s, stats_s)
        days = [_day(t) for t in range(int(start), int(now) + 3600, 3600)] + [_day(now)]
        for day in dict.fromkeys(days):
            self._read(self.dir / ("%s_%s.csv" % (self.cam, day)), lambda r: self._take_long(r, longs, lo_l, now, zone_ids))
            self._read(self.dir / ("%s_%s_stats.csv" % (self.cam, day)), lambda r: self._take_stats(r, stats, lo_s, now, zone_ids))
        return [(ts, longs[ts][0], longs[ts][1]) for ts in sorted(longs)], [[k, stats[k]] for k in sorted(stats)]

    @staticmethod
    def _read(path: Path, take):
        if not path.is_file():
            return
        try:
            with open(path, newline="", encoding="utf-8-sig") as fh:
                rd = csv.reader(fh, delimiter=";")
                next(rd, None)
                for r in rd:
                    try:
                        take(r)
                    except (ValueError, IndexError):
                        continue
        except OSError as e:
            log.warning("тренды не прочитаны (%s): %s", path.name, e)

    @staticmethod
    def _take_long(r, out, lo, now, zone_ids):
        ts = round(float(r[1]), 1)
        if ts < lo or ts > now + 5 or r[2] not in zone_ids:
            return
        z, ok = out.setdefault(ts, ({}, 1))
        z[r[2]] = (float(r[3]), float(r[4]), int(r[5]))
        out[ts] = (z, min(ok, int(r[6])))

    @staticmethod
    def _take_stats(r, out, lo, now, zone_ids):
        key = int(r[1])
        if key * 10 < lo or key * 10 > now + 5 or r[2] not in zone_ids:
            return
        v = [int(r[3])] + [float(x) for x in r[4:10]]
        out.setdefault(key, {})[r[2]] = v


# ───────────────────────────── снимки «в один момент» ─────────────────────────────
def write_shot(base: Path, ts: float, cams: dict) -> Optional[Path]:
    """cams = {id: {"jpeg": bytes|None, "info": dict}} -> папка снимка. Пишет JPEG, log.json и строки в сводный log.csv суток."""
    import json
    day, hms = _day(ts), time.strftime("%H%M%S", time.localtime(ts))
    folder = Path(base) / "shots" / day / hms
    folder.mkdir(parents=True, exist_ok=True)
    info = {}
    for cid, c in cams.items():
        if c.get("jpeg"):
            (folder / ("%s.jpg" % cid)).write_bytes(c["jpeg"])
        info[cid] = dict(c["info"], photo=bool(c.get("jpeg")))
    (folder / "log.json").write_text(json.dumps({"time": _stamp(ts), "cameras": info}, ensure_ascii=False, indent=1), encoding="utf-8")
    csv_path = folder.parent / "log.csv"
    new = not csv_path.is_file()
    with open(csv_path, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        if new:
            w.writerow(["time", "camera", "connected", "alive", "status", "fps", "zone", "flow", "diff", "bit", "photo"])
        for cid, i in info.items():
            zones = i.get("zones") or {}
            for zid, z in (zones or {"": {}}).items():
                w.writerow([_stamp(ts), cid, int(bool(i.get("connected"))), int(bool(i.get("alive"))), i.get("status", ""), i.get("fps", ""),
                            zid, z.get("flow", ""), z.get("diff", ""), z.get("bit", ""), int(i["photo"])])
    return folder


def purge(base: Path, days: float = KEEP_DAYS, now: Optional[float] = None) -> int:
    """Удалить записи, тренды и снимки старше days суток. -> сколько файлов/папок убрано."""
    now = now or time.time()
    edge = now - days * 86400
    n = 0
    base = Path(base)
    for sub in ("records", "trends"):
        for p in (base / sub).glob("*.csv") if (base / sub).is_dir() else []:
            try:
                if p.stat().st_mtime < edge:
                    p.unlink()
                    n += 1
            except OSError:
                pass
    shots = base / "shots"
    if shots.is_dir():
        for d in shots.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < edge and len(d.name) == 8 and d.name.isdigit():
                    shutil.rmtree(d)
                    n += 1
            except OSError:
                pass
    return n
