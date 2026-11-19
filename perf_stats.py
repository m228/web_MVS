"""perf_stats — сколько времени уходит на обработку одной картинки (кадра, пробы) в каждом месте, где она обрабатывается.

Один файл без зависимостей. Лежит в двух местах — в корне (приложение) и в motion_service/ (сервис движения живёт отдельным
процессом со своим окружением); тест следит, чтобы копии не разошлись.

    PERF.record("cam1:shafts", ms, got=2, of=2, budget_ms=333)

Что хранится по ключу: скользящее окно последних WINDOW замеров (мс) → последнее, среднее, p95, максимум; сколько кадров
посчитано всего; сколько нашлось (got) из скольких (of) — «палок 2 из 2», «объектов 14»; бюджет budget_ms — через сколько
приходит следующий кадр, и загрузка = среднее / бюджет (> 100 % — не успеваем). Накладные расходы — одна блокировка и
добавление в deque: доли микросекунды на замер.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional

WINDOW = 200            # сколько последних замеров помним по каждому ключу
STALE_S = 60.0          # дольше стольких секунд без новых замеров — источник «стоит» (в сводке age_s)


class Perf:
    def __init__(self, window: int = WINDOW):
        self.window = max(2, int(window))
        self._lock = threading.Lock()
        self._d: dict = {}

    def record(self, key: str, ms: float, got: Optional[float] = None, of: Optional[float] = None,
               budget_ms: Optional[float] = None, note: str = "") -> None:
        """Один замер: ms — сколько заняла обработка; got/of — что получено из скольких; budget_ms — интервал между кадрами."""
        now = time.time()
        with self._lock:
            e = self._d.get(key)
            if e is None:
                e = self._d[key] = {"win": deque(maxlen=self.window), "n": 0, "got": None, "of": None, "budget": None, "note": "", "t": 0.0}
            e["win"].append(float(ms))
            e["n"] += 1
            e["t"] = now
            if got is not None:
                e["got"] = got
            if of is not None:
                e["of"] = of
            if budget_ms:
                e["budget"] = float(budget_ms)
            if note:
                e["note"] = note

    def drop(self, prefix: str) -> None:
        """Забыть ключи, начинающиеся с prefix (камеру убрали из настроек)."""
        with self._lock:
            for k in [k for k in self._d if k.startswith(prefix)]:
                del self._d[k]

    def snapshot(self) -> dict:
        now = time.time()
        out = {}
        with self._lock:
            items = [(k, list(e["win"]), dict(e)) for k, e in self._d.items()]
        for k, win, e in items:
            if not win:
                continue
            s = sorted(win)
            avg = sum(win) / len(win)
            p95 = s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]
            b = e["budget"]
            out[k] = {"n": e["n"], "last": round(win[-1], 2), "avg": round(avg, 2), "p95": round(p95, 2), "max": round(s[-1], 2),
                      "got": e["got"], "of": e["of"], "budget_ms": round(b, 1) if b else None,
                      "load": round(100.0 * avg / b, 1) if b else None, "age_s": round(now - e["t"], 1), "note": e["note"]}
        return out


PERF = Perf()
