"""Задание «Подогнать валы»: запись вырезов вокруг осей → подгонка (motion_autocal) → результат для панели.

Живёт в Manager (по камере), а не в воркере: сохранение настроек пересоздаёт воркеры, а результат подгонки должен пережить это и ждать «Применить».
Запись не мешает боевому анализу: берём уже принятый воркером кадр (self._frame) раз в секунду и режем вырез по каждой оси.
Подгонка идёт в фоновом потоке, по одной оси за раз. Одновременно — одно задание на сервис (не перегружаем Server).
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Callable, Optional

import motion_autocal as ac

log = logging.getLogger("motion")

REC_STEP_S = 1.0                 # вырез в запись — раз в секунду (≈4,8° хода вала между кадрами)
DEFAULT_SECONDS, MIN_SECONDS, MAX_SECONDS = 150, 75, 300
NO_FRAME_S = 15.0                # камера столько не отдаёт новых кадров — задание прерываем
STORE_LIMIT = 420 * 1024 * 1024  # потолок памяти на записанные вырезы (JPEG) всех осей


def jsonable(x):
    """Результат подгонки → чистые python-типы (numpy в JSON не ложится)."""
    return json.loads(json.dumps(x, default=lambda o: o.item() if hasattr(o, "item") else (o.tolist() if hasattr(o, "tolist") else str(o))))


class AutocalJob(threading.Thread):
    def __init__(self, cid: str, get_worker: Callable, zone_ids: Optional[list] = None, seconds: float = DEFAULT_SECONDS):
        super().__init__(daemon=True, name="autocal-" + cid)
        self.cid = cid
        self.get_worker = get_worker
        self.want = list(zone_ids or [])
        self.seconds = max(MIN_SECONDS, min(MAX_SECONDS, float(seconds or DEFAULT_SECONDS)))
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self.st = {"state": "starting", "progress": 0.0, "text": "запуск", "camera": cid, "seconds": self.seconds, "zones": [],
                   "started": time.time(), "finished": None, "error": "", "results": {}, "applied": [], "frame": None}

    # — внешнее —
    def cancel(self):
        self._cancel.set()

    def status(self) -> dict:
        with self._lock:
            return jsonable(self.st)

    @property
    def running(self) -> bool:
        return self.is_alive() and self.st["state"] in ("starting", "recording", "tuning")

    def mark_applied(self, zids: list):
        with self._lock:
            self.st["applied"] = sorted(set(self.st["applied"]) | set(zids))

    def result_for(self, zid: str) -> Optional[dict]:
        with self._lock:
            r = self.st["results"].get(zid)
            return json.loads(json.dumps(r)) if r else None

    def _set(self, **kw):
        with self._lock:
            self.st.update(kw)

    def _is_cancelled(self) -> bool:
        return self._cancel.is_set()

    # — работа —
    def run(self):
        try:
            self._work()
        except ac.Cancelled:
            self._set(state="cancelled", text="отменено", finished=time.time())
        except Exception as e:  # noqa: BLE001 — любая поломка не должна ронять сервис; причина — в панель и журнал
            log.exception("[%s] автоподгонка валов не вышла", self.cid)
            self._set(state="error", error=str(e) or e.__class__.__name__, text="ошибка", finished=time.time())

    def _zones(self, w) -> list:
        zs = [z for z in w.cfg["zones"] if z["type"] == "cycle" and z["enabled"] and (not self.want or z["id"] in self.want)]
        if not zs:
            raise ValueError("у камеры нет включённых зон-палок%s" % (" с такими id" if self.want else ""))
        return zs

    def _work(self):
        w = self.get_worker(self.cid)
        if w is None:
            raise ValueError("нет такой камеры: %s" % self.cid)
        zones = self._zones(w)
        t_end = time.time() + NO_FRAME_S
        frame = None
        while frame is None:                                          # ждём первый кадр
            frame, stamp = w.autocal_frame()
            if frame is None:
                if self._is_cancelled():
                    raise ac.Cancelled()
                if time.time() > t_end:
                    raise ValueError("камера не отдаёт кадры")
                time.sleep(0.3)
        H, W = frame.shape[:2]
        fs = w.cfg.get("frame_size") or [W, H]
        sx, sy = W / float(fs[0]), H / float(fs[1])
        sr = (sx + sy) / 2.0
        geo = {}                                                       # зона → окно выреза и стартовые ось/радиус в родных пикселях
        for z in zones:
            cx, cy, R = z["center"][0] * sx, z["center"][1] * sy, z["radius"] * sr
            half = ac.RADIUS_MAX_K * R + max(40.0, 0.15 * R) + 20.0
            geo[z["id"]] = {"x0": max(0, int(cx - half)), "y0": max(0, int(cy - half)), "x1": min(W, int(cx + half)), "y1": min(H, int(cy + half)),
                            "c": (cx, cy), "R": R}
        maxr = {}
        for zid, g in geo.items():                                     # чужая палка не должна попадать в лучи: радиус ≤ 0,49 расстояния между осями
            others = [o for k, o in geo.items() if k != zid]
            maxr[zid] = min((0.49 * math.hypot(g["c"][0] - o["c"][0], g["c"][1] - o["c"][1]) for o in others), default=None)
        stores = {zid: ac.JpegStore(95) for zid in geo}
        self._set(state="recording", text="запись вращения", zones=[z["id"] for z in zones], frame=[W, H])
        t0 = time.time()
        last_stamp, last_new, last_add = stamp, time.time(), 0.0
        while True:
            if self._is_cancelled():
                raise ac.Cancelled()
            now = time.time()
            el = now - t0
            if el >= self.seconds:
                break
            self._set(progress=0.25 * el / self.seconds, text="запись вращения: %d из %d с" % (el, self.seconds))
            f, stamp = w.autocal_frame()
            if f is not None and stamp != last_stamp:
                last_stamp, last_new = stamp, now
                if now - last_add >= REC_STEP_S and f.shape[:2] == (H, W):
                    last_add = now
                    for zid, g in geo.items():
                        stores[zid].add(now - t0, f[g["y0"]:g["y1"], g["x0"]:g["x1"]])
                    if sum(s.nbytes for s in stores.values()) > STORE_LIMIT:
                        log.warning("[%s] запись для подгонки упёрлась в потолок памяти — останавливаю раньше", self.cid)
                        break
            elif now - last_new > NO_FRAME_S:
                raise ValueError("камера перестала отдавать кадры")
            time.sleep(0.1)
        n = min(len(s) for s in stores.values())
        if n < 40:
            raise ValueError("записано мало кадров (%d): камера отдаёт кадры слишком редко" % n)
        self._set(state="tuning", progress=0.25, text="подбор")
        results = {}
        for k, z in enumerate(zones):
            zid, g = z["id"], geo[z["id"]]
            lo, span = 0.25 + 0.75 * k / len(zones), 0.75 / len(zones)
            tn = ac.RodTuner(stores[zid], z, (g["c"][0] - g["x0"], g["c"][1] - g["y0"]), g["R"], max_radius=maxr[zid],
                             progress=lambda p, text, lo=lo, span=span, z=z: self._set(progress=lo + span * p, text="%s: %s" % (z["name"], text)),
                             cancelled=self._is_cancelled)
            res = tn.run()
            results[zid] = self._pack(z, res, g, sx, sy, sr)
            stores[zid] = None                                          # память освобождаем по мере готовности
            self._set(results=jsonable(results))
        self._set(state="done", progress=1.0, text="готово", finished=time.time(), results=jsonable(results))
        log.info("[%s] автоподгонка валов готова: %s", self.cid, {k: (v["improved"], round(v["after"]["found"], 2)) for k, v in results.items()})

    @staticmethod
    def _pack(z: dict, res: dict, g: dict, sx: float, sy: float, sr: float) -> dict:
        """Результат подбора → в координатах настроек зоны (кадр опорного размера frame_size), как лежат в конфиге."""
        new = res["new"]
        cfg_new = {"center": [round((new["center"][0] + g["x0"]) / sx, 1), round((new["center"][1] + g["y0"]) / sy, 1)],
                   "radius": round(new["radius"] / sr, 1), "red_hsv": new["red_hsv"], "blue_hsv": new["blue_hsv"],
                   "thin": new["thin"], "blue_alone": new["blue_alone"]}
        cfg_old = {"center": list(z["center"]), "radius": z["radius"], "red_hsv": list(z["red_hsv"]), "blue_hsv": list(z["blue_hsv"]),
                   "thin": z["thin"], "blue_alone": z["blue_alone"]}
        return {"name": z["name"], "old": cfg_old, "new": cfg_new, "improved": res["improved"], "gain_val": res["gain_val"],
                "before": res["before"], "after": res["after"], "before_val": res["before_val"], "after_val": res["after_val"],
                "frames": res["frames"], "seconds": res["seconds"], "log": res["log"]}
