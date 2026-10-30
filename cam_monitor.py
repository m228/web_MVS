"""Монитор камер: раз в секунду смотрит на RTSP-камеры и сервис КЗ и пишет эпизоды в cam_events.

Работает в фоне самого сервера, без открытой страницы. Источники:
  • RTSP-воркеры web_MVS: переподключение (link_lost), серые кадры (gray);
  • сервис КЗ (/state): сбои анализа (cv_dead / cv_frozen / cv_corrupt) и зоны «крутится/стоит» (zone_on / zone_off).
Пока КЗ без данных, зоны не пишем — состояние зоны неизвестно, и «стоит» там было бы враньём.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable, Optional

import cv2
import numpy as np

import cam_events
from camera_core.frame_quality import GARBAGE_THRESHOLD, frame_garbage
from logger import log_event

RTSP_KINDS = ("link_lost", "gray")
CV_KINDS = ("cv_dead", "cv_frozen", "cv_corrupt", "zone_on", "zone_off")
MOTION_RETRY_S = 10.0        # сервис КЗ не отвечает — следующий опрос не раньше, чем через столько (не копим таймауты)
PURGE_EVERY_S = 6 * 3600

# Снимок по событию: остановка зоны или зависание картинки, удержавшиеся hold_s секунд. Серые/неполные кадры (мигание «каши»)
# отсекаются тремя заслонами: событие должно продержаться, сам кадр проверяется на серую кашу, плюс пауза и суточный лимит.
SHOT_KINDS = ("zone_off", "cv_frozen")
SHOT_DEFAULTS = {"enabled": True, "hold_s": 30.0, "cooldown_s": 300.0, "daily_limit": 100}
SHOT_TRIES = 3             # сколько раз пробуем взять кадр одного события (кадр мог оказаться кашей)
SHOT_RETRY_S = 3.0


class Monitor:
    def __init__(self, workers_fn: Callable[[], dict], motion_url_fn: Callable[[], str],
                 motion_state_fn: Callable[[str], dict], clock: Callable[[], float] = time.time,
                 shot_fn: Optional[Callable] = None, cfg_fn: Optional[Callable[[], dict]] = None, async_shots: bool = True):
        self.shot_fn = shot_fn                  # (url, камера) -> (jpeg, w, h) | None; без него снимков нет
        self.cfg_fn = cfg_fn
        self.async_shots = async_shots
        self._shot_cfg = dict(SHOT_DEFAULTS)
        self._shot_cfg_at = -1e9
        self._since: dict = {}                  # (камера, kind, зона) -> когда событие замечено
        self._shot_done: set = set()
        self._shot_try: dict = {}               # ключ -> (сколько попыток, не раньше какого момента)
        self._last_shot: dict = {}              # (камера, зона) -> время последнего снимка
        self._day_count: dict = {}              # (камера, дата) -> сколько снимков сегодня
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
            self._forget_shots()
            if not isinstance(st, dict) or "error" in st:
                self._motion_retry_at = now + MOTION_RETRY_S
            return
        seen = set()
        for c in cams:
            cid = c.get("id")
            if not cid:
                continue
            seen.add(cid)
            active = self._cv_active(c)
            cam_events.sync(cid, CV_KINDS, active, now)
            self._shots_for(cid, active, now)
        for gone in self._cv_seen - seen:
            cam_events.sync(gone, CV_KINDS, {}, now)
            self._forget_shots(gone)
        self._cv_seen = seen

    # ---------- снимок по событию ----------
    def _cfg(self, now: float) -> dict:
        if self.cfg_fn and now - self._shot_cfg_at >= 30:
            self._shot_cfg_at = now
            try:
                self._shot_cfg = {**SHOT_DEFAULTS, **(self.cfg_fn() or {})}
            except Exception:
                self._shot_cfg = dict(SHOT_DEFAULTS)
        return self._shot_cfg

    def _forget_shots(self, cid: Optional[str] = None) -> None:
        """Связь с сервисом КЗ пропала: «событие держится N секунд» отсчитываем заново, когда он вернётся."""
        for key in [k for k in self._since if cid is None or k[0] == cid]:
            self._since.pop(key, None)
            self._shot_done.discard(key)
            self._shot_try.pop(key, None)

    def _shots_for(self, cid: str, active: dict, now: float) -> None:
        if not self.shot_fn:
            return
        cfg = self._cfg(now)
        keys = {(cid, k, z) for (k, z) in active if k in SHOT_KINDS}
        for key in [k for k in self._since if k[0] == cid and k not in keys]:      # событие кончилось — забываем
            self._since.pop(key, None)
            self._shot_done.discard(key)
            self._shot_try.pop(key, None)
        if not cfg.get("enabled"):
            return
        for key in keys:
            since = self._since.setdefault(key, now)
            if key in self._shot_done or now - since < float(cfg["hold_s"]):
                continue
            tries, not_before = self._shot_try.get(key, (0, 0.0))
            if tries >= SHOT_TRIES or now < not_before:
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(now))
            if now - self._last_shot.get((cid, key[2]), -1e9) < float(cfg["cooldown_s"]) \
                    or self._day_count.get((cid, day), 0) >= int(cfg["daily_limit"]):
                self._shot_done.add(key)                                           # пауза/лимит: это событие пропускаем
                continue
            self._shot_try[key] = (tries + 1, now + SHOT_RETRY_S)
            if self.async_shots:
                threading.Thread(target=self._take_shot, args=(key, now, day), daemon=True, name="cam-shot").start()
            else:
                self._take_shot(key, now, day)

    def _take_shot(self, key: tuple, now: float, day: str) -> bool:
        cid, kind, zone = key
        try:
            r = self.shot_fn(self.motion_url_fn(), cid)
            if not r:
                return False
            img = cv2.imdecode(np.frombuffer(r[0], np.uint8), cv2.IMREAD_COLOR)
            if img is None or frame_garbage(img) >= GARBAGE_THRESHOLD:
                return False                                                       # серая каша — не сохраняем, попробуем позже
            ep_id = cam_events.open_id(cid, kind, zone)
            if ep_id is None:
                return False
            safe = lambda t: re.sub(r"[^A-Za-z0-9_.-]+", "_", str(t)).strip("_") or "x"
            rel = "%s/%s/%s_%s_%s.jpg" % (safe(cid), day, time.strftime("%H%M%S", time.localtime(now)), kind, safe(zone or "cam"))
            path = cam_events.shots_dir() / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r[0])
            cam_events.set_shot(ep_id, rel)
            self._shot_done.add(key)
            self._last_shot[(cid, zone)] = now
            self._day_count[(cid, day)] = self._day_count.get((cid, day), 0) + 1
            return True
        except Exception as e:
            log_event("cam_monitor", "Не удалось сохранить кадр события", "warn", {"cam": cid, "kind": kind, "error": repr(e)})
            return False

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
