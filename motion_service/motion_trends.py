"""trends — тренды и снимки «в один момент», чтобы они переживали перезапуск и обновление сервиса.

Тренды пишутся в SQLite (motion_store, data\\motion.db): точка раз в секунду на зону (сырое, хранится KEEP_DAYS) и корзина 10 с на зону
(среднее / мин / макс; хранится навсегда — на порядок компактнее). При старте камеры они читаются обратно в память — графики и
«среднее / мин / макс» не обнуляются после обновления. Старые CSV (data\\trends\\*.csv, до версии 2.2) за последние двое суток
переносятся в базу при первом запуске, остальные убираются по возрасту.

Снимки: раз в N минут (по умолчанию 20, по часам: :00 :20 :40) все камеры снимаются одновременно:
  shots\\<ГГГГММДД>\\<ЧЧММСС>\\<камера>.jpg + log.json (значения зон, связь, статус); сводка по зонам — в таблице shot базы (навсегда).
На кадре нарисованы зоны (зелёная — «движется», красная — «стоит») и текущие diff/flow. JPEG старше SHOTS_KEEP_DAYS суток удаляются (purge).
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Optional

from motion_store import store_for

log = logging.getLogger("motion.trends")

KEEP_DAYS = 60                         # сырые секундные тренды и записи по кадрам; корзины статистики и сводка снимков — навсегда
SHOTS_KEEP_DAYS = 10                   # JPEG снимков (самое тяжёлое на диске)
MIN_FREE_BYTES = 1 << 30               # свободного места меньше — сырое режем до LOW_DISK_DAYS, снимки не делаем
LOW_DISK_DAYS = 7


def _day(ts: float) -> str:
    return time.strftime("%Y%m%d", time.localtime(ts))


class TrendLog:
    """Тренды одной камеры: запись (add_*) и чтение при старте (load). Ошибки диска камеру не ломают."""

    def __init__(self, base: Path, cam_id: str):
        self.cam = cam_id
        self.store = store_for(base)
        self._closed = False
        self._warned = False

    def _guard(self, fn):
        if self._closed:
            return
        try:
            fn()
        except (OSError, sqlite3.Error) as e:
            if not self._warned:
                self._warned = True
                log.warning("[%s] тренды на диск не пишутся: %s", self.cam, e)

    def add_long(self, ts: float, zones: dict, ok: int):
        """Секундная точка: zones = {зона: (flow, diff, bit)}."""
        def go():
            self.store.add_trend(self.cam, ts, zones, ok)
            self.store.maybe_flush(ts)
        self._guard(go)

    def add_stats(self, bucket: list):
        """Корзина статистики: [ключ 10-секундки, {зона: [n, Σflow, min, max, Σdiff, min, max]}]."""
        def go():
            self.store.add_stat(self.cam, bucket[0], bucket[1])
            self.store.maybe_flush(bucket[0] * 10.0)
        self._guard(go)

    def close(self):
        self._closed = True
        try:
            self.store.flush()
        except (OSError, sqlite3.Error):
            pass

    def load(self, now: float, long_s: float, stats_s: float, zone_ids: set):
        """-> (длинная история [(ts, {зона: (flow, diff, bit)}, ok)], корзины статистики [[ключ, {зона: [..7..]}]]) за последние
        long_s / stats_s секунд; зоны, которых больше нет в настройках, пропускаются."""
        return self.store.load_trend(self.cam, now, long_s, stats_s, zone_ids)


# ───────────────────────────── снимки «в один момент» ─────────────────────────────
def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def write_shot(base: Path, ts: float, cams: dict) -> Optional[Path]:
    """cams = {id: {"jpeg": bytes|None, "info": dict}} -> папка снимка. Пишет JPEG, log.json и строки сводки в таблицу shot."""
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
    store = store_for(base)
    for cid, i in info.items():
        zones = i.get("zones") or {}
        for zid, z in (zones or {"": {}}).items():
            store.add_shot(ts, cid, zid, bool(i.get("connected")), bool(i.get("alive")), i.get("status", ""), i.get("fps", ""),
                           z.get("flow", ""), z.get("diff", ""), z.get("bit", ""), i["photo"])
    return folder


def purge(base: Path, days: float = KEEP_DAYS, now: Optional[float] = None, shots_days: Optional[float] = None, vacuum_pages: int = 5000) -> int:
    """Убрать сырое старше days суток (секундные тренды, кадры записи в базе; старые CSV файлами), JPEG снимков — старше shots_days
    (по умолчанию days). Корзины статистики и сводка снимков остаются. -> сколько строк/файлов/папок убрано."""
    now = now or time.time()
    n = 0
    base = Path(base)
    n += store_for(base).purge_raw(now - days * 86400, vacuum_pages)
    edge = now - days * 86400
    for sub in ("records", "trends"):                      # CSV прежних версий
        for p in (base / sub).glob("*.csv") if (base / sub).is_dir() else []:
            try:
                if p.stat().st_mtime < edge:
                    p.unlink()
                    n += 1
            except OSError:
                pass
    edge = now - (days if shots_days is None else shots_days) * 86400
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


def low_disk(base: Path) -> bool:
    try:
        return shutil.disk_usage(str(base)).free < MIN_FREE_BYTES
    except OSError:
        return False
