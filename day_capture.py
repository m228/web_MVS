"""Суточная съёмка новой RTSP-камеры: раз в 20 минут кадр в полном размере, ровно сутки — набор дневных и ночных кадров для разбора и дообучения.

Камера считается новой, когда она появилась в базе RTSP (rtsp_store) — приложение сохраняет её туда, когда подключение реально удалось
(см. saveRtspIfNeeded в page/static/js/multi/04-stream.js). Сутки отсчитываются от первого удачного кадра. Кадры лежат в
data\\day_shots\\<камера>\\<ГГГГ-ММ-ДД>\\<ЧЧММСС>.jpg, папки дней старше KEEP_DAYS суток удаляются сами.

При первом запуске (файла состояния ещё нет) все камеры, что уже стоят в базе, записываются как «известные» и не снимаются: иначе в день обновления
на заводе все старые камеры разом начали бы суточную съёмку. Состояние (что уже снято, когда следующий кадр) живёт в data\\day_capture.json
и переживает перезапуск приложения.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import cv2

from camera_core.frame_quality import GARBAGE_THRESHOLD, frame_garbage
from logger import log_event

STEP_S = 20 * 60              # шаг съёмки
SPAN_S = 24 * 3600            # сколько длится съёмка одной камеры, от первого удачного кадра
KEEP_DAYS = 7                 # сколько суток хранятся кадры (по дате папки дня)
RETRY_S = 60.0                # кадр не вышел (камера не отвечает, серая каша) — повтор через столько
GIVE_UP_S = 7 * 86400         # камера так и не дала ни одного кадра за столько — отменяем съёмку
LOOP_S = 15.0                 # как часто фоновый поток смотрит, не пора ли снимать
PURGE_EVERY_S = 6 * 3600
JPEG_QUALITY = 95

_DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _safe(text: str) -> str:
    return re.sub(r"[^\w.-]+", "_", str(text or ""), flags=re.UNICODE).strip("_.") or "cam"


class DayCapture:
    """store_fn() -> список камер [{url, serial, label}]; grab_fn(камера) -> BGR-кадр | None (полный размер, без цифрового зума)."""

    def __init__(self, store_fn: Callable[[], list], grab_fn: Callable[[dict], object], state_path: Path, out_dir: Path,
                 clock: Callable[[], float] = time.time, step_s: float = STEP_S, span_s: float = SPAN_S, keep_days: int = KEEP_DAYS):
        self.store_fn, self.grab_fn, self.clock = store_fn, grab_fn, clock
        self.state_path, self.out_dir = Path(state_path), Path(out_dir)
        self.step_s, self.span_s, self.keep_days = step_s, span_s, keep_days
        self._plans: dict = {}                  # url камеры -> план съёмки
        self._loaded = False
        self._last_purge = 0.0
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---------- состояние ----------
    def _load(self, cams: list, now: float) -> None:
        """Прочитать состояние. Нет файла (первый запуск) или он испорчен — все камеры из базы считаем известными, а не новыми."""
        self._loaded = True
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            plans = data["plans"]
            if not isinstance(plans, dict):
                raise ValueError("plans")
            self._plans = plans
            return
        except Exception:
            self._plans = {c["url"]: self._plan(c, now, done=True) for c in cams}
            self._save()

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.parent / (self.state_path.name + ".tmp")
            tmp.write_text(json.dumps({"plans": self._plans}, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except Exception as e:
            log_event("day_capture", "Не удалось сохранить состояние суточной съёмки", "warn", {"error": repr(e)})

    @staticmethod
    def _plan(c: dict, now: float, done: bool = False) -> dict:
        return {"serial": c.get("serial") or "", "label": c.get("label") or "", "added": now, "start": None, "next": 0.0,
                "count": 0, "done": done}

    def _sync(self, cams: list, now: float) -> bool:
        """Новые камеры базы — в план; удалённые из базы — из плана. True, если что-то поменялось."""
        urls = {c["url"] for c in cams}
        changed = False
        for c in cams:
            p = self._plans.get(c["url"])
            if p is None:
                self._plans[c["url"]] = self._plan(c, now)
                log_event("day_capture", "Новая камера: суточная съёмка раз в %d мин" % round(self.step_s / 60), "info",
                          {"camera": c.get("label") or c.get("serial")})
                changed = True
            elif (c.get("label") or "") != p.get("label") or (c.get("serial") or "") != p.get("serial"):
                p["label"], p["serial"] = c.get("label") or "", c.get("serial") or ""       # переименовали — новые кадры в папку с новым именем
                changed = True
        for url in [u for u in self._plans if u not in urls]:
            del self._plans[url]
            changed = True
        return changed

    # ---------- съёмка ----------
    def run_once(self, now: Optional[float] = None) -> int:
        """Один проход: обновить список камер, снять те, кому пора, раз в 6 ч почистить старое. Возвращает, сколько кадров снято."""
        now = self.clock() if now is None else now
        cams = [c for c in (self.store_fn() or []) if c.get("url")]
        with self._lock:
            if not self._loaded:
                self._load(cams, now)
            changed = self._sync(cams, now)
            if changed:
                self._save()
            by_url = {c["url"]: c for c in cams}
            due = [(u, p) for u, p in self._plans.items() if not p["done"] and now >= p["next"]]
        shots = 0
        for url, p in due:
            if self._shoot(by_url[url], p, now):
                shots += 1
        if due:
            with self._lock:
                self._save()
        if now - self._last_purge >= PURGE_EVERY_S:
            self._last_purge = now
            self.purge(now)
        return shots

    def _shoot(self, cam: dict, p: dict, now: float) -> bool:
        ok = False
        try:
            img = self.grab_fn(cam)
            ok = img is not None and frame_garbage(img) < GARBAGE_THRESHOLD       # серая каша — не кадр: повторим через минуту
            if ok:
                ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok:
                t = time.localtime(now)
                path = self.out_dir / _safe(p.get("label") or p.get("serial")) / time.strftime("%Y-%m-%d", t) / (time.strftime("%H%M%S", t) + ".jpg")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(buf.tobytes())
        except Exception as e:
            ok = False
            log_event("day_capture", "Не удалось снять кадр суточной съёмки", "warn", {"camera": p.get("label") or p.get("serial"), "error": repr(e)})
        with self._lock:
            if ok:
                if p["start"] is None:
                    p["start"] = now
                p["count"] += 1
                p["next"] = now + self.step_s
                if p["next"] - p["start"] > self.span_s:
                    p["done"] = True
                    log_event("day_capture", "Суточная съёмка камеры закончена", "info",
                              {"camera": p.get("label") or p.get("serial"), "frames": p["count"], "folder": str(self.out_dir / _safe(p.get("label") or p.get("serial")))})
            else:
                p["next"] = now + RETRY_S
                if p["start"] is None and now - p["added"] > GIVE_UP_S:
                    p["done"] = True
                    log_event("day_capture", "Камера не дала ни одного кадра за неделю — суточная съёмка отменена", "warn",
                              {"camera": p.get("label") or p.get("serial")})
        return bool(ok)

    # ---------- хранение ----------
    def purge(self, now: float) -> int:
        """Удалить папки дней старше keep_days суток. Трогаем только <камера>/<ГГГГ-ММ-ДД> со строгим именем-датой; опустевшие папки камер тоже."""
        if not self.out_dir.is_dir():
            return 0
        cutoff = datetime.fromtimestamp(now - self.keep_days * 86400).strftime("%Y-%m-%d")
        n = 0
        for cam_dir in (p for p in self.out_dir.iterdir() if p.is_dir()):
            for day in (p for p in cam_dir.iterdir() if p.is_dir() and _DAY_DIR.match(p.name)):
                if day.name < cutoff:
                    shutil.rmtree(day, ignore_errors=True)
                    n += 1
            try:
                cam_dir.rmdir()                  # только если пуста
            except OSError:
                pass
        if n:
            log_event("day_capture", "Старые кадры суточной съёмки удалены", "info", {"days": n, "keep_days": self.keep_days})
        return n

    # ---------- фоновый поток ----------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._halt.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="day-capture")
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._halt.is_set():
            try:
                self.run_once()
            except Exception as e:               # съёмка вспомогательная: падать не должна
                log_event("day_capture", "Сбой прохода суточной съёмки", "warn", {"error": repr(e)})
            self._halt.wait(LOOP_S)
