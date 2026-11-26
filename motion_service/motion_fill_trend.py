"""Тренды признака «вид окна» (зернистость tex, яркость lum, яркость к металлу rel): график и среднее / мин / макс за период.

Отдельно от тренда flow / diff (там тройка (flow, diff, bit) и таблицы в базе), чтобы не трогать их формат. Копится только в памяти:
после перезапуска сервиса начинается заново. Копятся только зоны с включённым признаком (в состоянии зоны есть tex).

  длинная история — одна точка в секунду (среднее за секунду) на 2 часа назад, для графика;
  корзины статистики — 10 секунд на сутки назад, для среднее / мин / макс (только кадры в норме)."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional

LONG_LEN = 7200          # секундных точек: 2 часа
STATS_STEP_S = 10        # корзина статистики, с
STATS_LEN = 8640         # корзин: сутки
METRICS = ("tex", "lum", "rel")


_REG: dict = {}
_REG_LOCK = threading.Lock()


def fill_trend_for(key, sigs: dict) -> "FillTrend":
    """Тренд камеры. Правка настроек пересоздаёт камеру, а пороги как раз и подбирают по этим графикам, поэтому накопленное живёт в реестре
    и переживает пересоздание (стирается только перезапуском сервиса). sigs = {зона: подпись настроек окна, от которых зависят сами значения
    (полигон, признак)}: у зоны с другой подписью ряд начинается заново, у пропавших зон — удаляется; пороги и инверсия в подпись не входят."""
    with _REG_LOCK:
        ft = _REG.get(key)
        if ft is None:
            ft = _REG[key] = FillTrend()
    ft.sync(sigs)
    return ft


class FillTrend:
    def __init__(self, long_len: int = LONG_LEN, stats_len: int = STATS_LEN):
        self._lock = threading.Lock()
        self._sig: dict = {}
        self._long: deque = deque(maxlen=long_len)      # (ts, {зона: (tex, lum, rel, fill)}, ok)
        self._sec: Optional[list] = None                # набирающаяся секунда: [ts, {зона: [n, Σtex, Σlum, Σrel, fill]}, ok]
        self._stats: deque = deque(maxlen=stats_len)    # [ключ 10 с, {зона: [n, Σt, min, max, Σl, min, max, Σr, min, max]}]
        self._sbucket: Optional[list] = None

    def sync(self, sigs: dict):
        """Сверить подписи зон с прошлыми: изменившиеся и пропавшие зоны забывают накопленное."""
        with self._lock:
            gone = {z for z, s in self._sig.items() if z not in sigs or sigs[z] != s}
            if gone:
                self._long = deque(((ts, {z: v for z, v in zs.items() if z not in gone}, ok) for ts, zs, ok in self._long), maxlen=self._long.maxlen)
                self._stats = deque(([k, {z: v for z, v in zs.items() if z not in gone}] for k, zs in self._stats), maxlen=self._stats.maxlen)
                if self._sec is not None:
                    self._sec[1] = {z: v for z, v in self._sec[1].items() if z not in gone}
                if self._sbucket is not None:
                    self._sbucket[1] = {z: v for z, v in self._sbucket[1].items() if z not in gone}
            self._sig = dict(sigs)

    def add(self, ts: float, zones: dict, ok: bool):
        """Кадр. zones = {зона: (tex, lum, rel, fill|None)}; ok — кадр в норме (в статистику идут только такие)."""
        if not zones:
            return
        with self._lock:
            s = self._sec
            if s is not None and ts - s[0] >= 1.0:
                self._long.append(self._finish(s))
                s = self._sec = None
            if s is None:
                s = self._sec = [ts, {}, 1 if ok else 0]
            else:
                s[2] = min(s[2], 1 if ok else 0)
            for zid, (tex, lum, rel, fill) in zones.items():
                a = s[1].get(zid)
                if a is None:
                    s[1][zid] = [1, tex, lum, rel, fill]
                else:
                    a[0] += 1; a[1] += tex; a[2] += lum; a[3] += rel
                    if fill is not None:
                        a[4] = fill
            if ok:
                self._stats_add(ts, zones)

    @staticmethod
    def _finish(s: list) -> tuple:
        return (s[0], {z: (a[1] / a[0], a[2] / a[0], a[3] / a[0], a[4]) for z, a in s[1].items()}, s[2])

    def _stats_add(self, ts: float, zones: dict):
        key = int(ts // STATS_STEP_S)
        b = self._sbucket
        if b is None or b[0] != key:
            if b is not None:
                self._stats.append(b)
            b = self._sbucket = [key, {}]
        for zid, (tex, lum, rel, _fill) in zones.items():
            a = b[1].get(zid)
            if a is None:
                b[1][zid] = [1, tex, tex, tex, lum, lum, lum, rel, rel, rel]
            else:
                a[0] += 1
                a[1] += tex; a[2] = min(a[2], tex); a[3] = max(a[3], tex)
                a[4] += lum; a[5] = min(a[5], lum); a[6] = max(a[6], lum)
                a[7] += rel; a[8] = min(a[8], rel); a[9] = max(a[9], rel)

    def history(self, window_s: float, points: int = 600) -> dict:
        """Ряды для графиков: {t, ok, zones: {зона: {tex, lum, rel, fill}}}; правый край — последняя точка. Пусто — {}."""
        with self._lock:
            rows = list(self._long)
            if self._sec is not None:
                rows.append(self._finish(self._sec))
        if not rows:
            return {}
        edge = rows[-1][0] - float(window_s)
        rows = [r for r in rows if r[0] >= edge]
        points = max(10, int(points))
        k = max(1, -(-len(rows) // points))
        t, ok = [], []
        zones: dict = {}
        out = []
        for i in range(0, len(rows), k):
            chunk = rows[i:i + k]
            acc: dict = {}
            for _ts, zs, _ok in chunk:
                for zid, (tex, lum, rel, fill) in zs.items():
                    a = acc.get(zid)
                    if a is None:
                        acc[zid] = [1, tex, lum, rel, fill]
                    else:
                        a[0] += 1; a[1] += tex; a[2] += lum; a[3] += rel
                        if fill is not None:
                            a[4] = fill
            out.append((chunk[-1][0], acc, min(r[2] for r in chunk)))
        ids = {zid for _t, acc, _o in out for zid in acc}
        for zid in ids:
            zones[zid] = {m: [] for m in METRICS + ("fill",)}
        for ts, acc, o in out:
            t.append(round(ts, 2))
            ok.append(o)
            for zid in ids:
                a = acc.get(zid)
                d = zones[zid]
                if a is None:
                    for m in METRICS + ("fill",):
                        d[m].append(None)
                else:
                    d["tex"].append(round(a[1] / a[0], 3))
                    d["lum"].append(round(a[2] / a[0], 2))
                    d["rel"].append(round(a[3] / a[0], 3))
                    d["fill"].append(a[4])
        return {"t": t, "ok": ok, "zones": zones}

    def stats(self, window_s: float, now: Optional[float] = None) -> dict:
        """{зона: {n, tex: {avg, min, max}, lum: {...}, rel: {...}}} за последние window_s секунд (только кадры в норме)."""
        edge = int(((now if now is not None else time.time()) - float(window_s)) // STATS_STEP_S)
        with self._lock:
            rows = [r for r in self._stats if r[0] >= edge]
            if self._sbucket is not None and self._sbucket[0] >= edge:
                rows.append([self._sbucket[0], {k: list(v) for k, v in self._sbucket[1].items()}])
        acc: dict = {}
        for _key, zs in rows:
            for zid, v in zs.items():
                a = acc.get(zid)
                if a is None:
                    acc[zid] = list(v)
                else:
                    a[0] += v[0]
                    for j in (1, 4, 7):
                        a[j] += v[j]
                        a[j + 1] = min(a[j + 1], v[j + 1])
                        a[j + 2] = max(a[j + 2], v[j + 2])
        return {zid: {"n": a[0],
                      "tex": {"avg": round(a[1] / a[0], 3), "min": round(a[2], 3), "max": round(a[3], 3)},
                      "lum": {"avg": round(a[4] / a[0], 2), "min": round(a[5], 2), "max": round(a[6], 2)},
                      "rel": {"avg": round(a[7] / a[0], 3), "min": round(a[8], 3), "max": round(a[9], 3)}}
                for zid, a in acc.items() if a[0]}
