"""Монитор камер: раз в секунду смотрит на RTSP-камеры и сервис КЗ и пишет эпизоды в cam_events.

Работает в фоне самого сервера, без открытой страницы. Источники:
  • RTSP-воркеры web_MVS: переподключение (link_lost), серые кадры (gray);
  • сервис КЗ (/state): сбои анализа (cv_dead / cv_frozen / cv_corrupt) и зоны «крутится/стоит» (zone_on / zone_off).
Пока КЗ без данных, зоны не пишем — состояние зоны неизвестно, и «стоит» там было бы враньём.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import cam_events
from logger import log_event

RTSP_KINDS = ("link_lost", "gray")
CV_KINDS = ("cv_dead", "cv_frozen", "cv_corrupt", "zone_on", "zone_off")
MOTION_RETRY_S = 10.0        # сервис КЗ не отвечает — следующий опрос не раньше, чем через столько (не копим таймауты)
PURGE_EVERY_S = 6 * 3600


class Monitor:
    def __init__(self, workers_fn: Callable[[], dict], motion_url_fn: Callable[[], str],
                 motion_state_fn: Callable[[str], dict], clock: Callable[[], float] = time.time):
        self.workers_fn = workers_fn
        self.motion_url_fn = motion_url_fn
        self.motion_state_fn = motion_state_fn
        self.clock = clock
        self._rtsp_seen: set = set()
        self._cv_seen: set = set()
        self._motion_retry_at = 0.0
        self._last_purge = 0.0
        self._halt = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---------- один проход (отдельно от потока — удобно для тестов) ----------
    def tick(self) -> None:
        now = self.clock()
        self._tick_rtsp(now)
        self._tick_motion(now)
        cam_events.heartbeat(now)
        if now - self._last_purge >= PURGE_EVERY_S:
            self._last_purge = now
            cam_events.purge(now)

    def _tick_rtsp(self, now: float) -> None:
        workers = dict(self.workers_fn() or {})
        for serial, w in workers.items():
            active = {}
            # reconnect_attempts растёт сразу после неудачи, reconnecting — только после паузы перед следующей попыткой
            if getattr(w, "reconnecting", False) or getattr(w, "reconnect_attempts", 0) > 0:
                active[("link_lost", "")] = ""
            if getattr(w, "gray_now", False):
                active[("gray", "")] = "%d%% кадра" % round(float(getattr(w, "gray_ratio", 0.0)) * 100)
            cam_events.sync(serial, RTSP_KINDS, active, now)
        for gone in self._rtsp_seen - set(workers):
            cam_events.sync(gone, RTSP_KINDS, {}, now)
        self._rtsp_seen = set(workers)

    def _tick_motion(self, now: float) -> None:
        if now < self._motion_retry_at:
            return
        st = self.motion_state_fn(self.motion_url_fn())
        cams = st.get("cameras") if isinstance(st, dict) else None
        if not cams:
            # сервис не запущен или камер нет: что шло — закрываем, повторный опрос не сразу
            for cid in self._cv_seen:
                cam_events.sync(cid, CV_KINDS, {}, now)
            self._cv_seen = set()
            if not isinstance(st, dict) or "error" in st:
                self._motion_retry_at = now + MOTION_RETRY_S
            return
        seen = set()
        for c in cams:
            cid = c.get("id")
            if not cid:
                continue
            seen.add(cid)
            cam_events.sync(cid, CV_KINDS, self._cv_active(c), now)
        for gone in self._cv_seen - seen:
            cam_events.sync(gone, CV_KINDS, {}, now)
        self._cv_seen = seen

    @staticmethod
    def _cv_active(c: dict) -> dict:
        """Что идёт у камеры в КЗ прямо сейчас: либо сбой анализа, либо по эпизоду на каждую включённую зону."""
        if not c.get("enabled"):
            return {}
        res = c.get("result")
        if not res:
            return {("cv_dead", ""): c.get("error") or "нет данных"}
        if res.get("corrupt"):
            return {("cv_corrupt", ""): res.get("reason") or "битый поток"}
        if not res.get("alive"):
            reason = res.get("reason") or c.get("error") or "нет связи"
            return {("cv_frozen" if "застыл" in reason else "cv_dead", ""): reason}
        return {("zone_on" if z.get("bit") else "zone_off", str(z.get("id"))): z.get("name") or str(z.get("id"))
                for z in res.get("zones") or [] if z.get("enabled", True)}

    # ---------- поток ----------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        n = cam_events.recover()
        if n:
            log_event("cam_monitor", "Незавершённые события прошлого запуска закрыты", "info", {"count": n})
        self._halt.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="cam-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        if self._thread:
            self._thread.join(timeout=3)
        # корректно завершаем идущие эпизоды, чтобы не висели «идущими» до следующего запуска
        now = self.clock()
        for cid in self._rtsp_seen:
            cam_events.sync(cid, RTSP_KINDS, {}, now)
        for cid in self._cv_seen:
            cam_events.sync(cid, CV_KINDS, {}, now)

    def _run(self) -> None:
        while not self._halt.is_set():
            t0 = time.time()
            try:
                self.tick()
            except Exception as e:      # монитор не должен падать: он вспомогательный
                log_event("cam_monitor", "Сбой прохода монитора камер", "warn", {"error": repr(e)})
            self._halt.wait(max(0.1, 1.0 - (time.time() - t0)))
