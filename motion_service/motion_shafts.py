"""Четыре вала на двух камерах: скорости и разности между валами (НЛВ, НПВ, ВЛВ, ВПВ).

Н/В — нижний/верхний ряд (поле камеры «ряд»), Л/П — левый/правый вал в ряду (по положению оси в кадре). Шесть букв не придумка:
НЛВ = Нижний Левый Вал, ВПВ = Верхний Правый Вал и так далее.

Четыре пары «рамкой»:
  nlr  НЛВ–НПВ — левый и правый нижнего ряда (крутятся навстречу: углы зеркальны, как у группы mirror)
  vlr  ВЛВ–ВПВ — то же у верхнего ряда
  lnv  НЛВ–ВЛВ — левые валы двух рядов (углы совпадают с точностью до эталона)
  rnv  НПВ–ВПВ — правые валы двух рядов

Для каждой пары считаем две разности:
  rpm_diff   — |скорость первого| − |скорость второго|, об/мин (направление вращения у левого и правого разное, поэтому по модулю);
  angle_diff — отклонение углов от эталона ref, градусы −180..180. Камеры смотрят под разными ракурсами, поэтому «ноль» разности
               углов снимается вручную (calibrate), когда валы стоят в нужном положении: ref = текущее значение.

Расчёт compute() — чистая функция «конфиг + состояния камер → снимок», без потоков и сети (проверяется тестами).
ShaftRunner раз в секунду зовёт её, ведёт историю для графика и пишет разности в ПЛК тем же каналом, что и сценарии.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

from motion_engine import PAIR_IDS, SHAFT_IDS, Hysteresis, ang_diff, normalize_shafts

log = logging.getLogger("motion.shafts")

ROWS = ("lower", "upper")
ROW_LETTER = {"lower": "Н", "upper": "В"}
# id вала → (ряд, сторона); подпись = буква ряда + буква стороны + «В»
SHAFTS = {"nl": ("lower", "L"), "np": ("lower", "R"), "vl": ("upper", "L"), "vp": ("upper", "R")}
SHAFT_ORDER = SHAFT_IDS
SIDE_LETTER = {"L": "Л", "R": "П"}
# id пары → (вал A, вал B, режим угла): mirror — углы зеркальны (сумма), same — углы одинаковы (разность)
PAIRS = {"nlr": ("nl", "np", "mirror"), "vlr": ("vl", "vp", "mirror"),
         "lnv": ("nl", "vl", "same"), "rnv": ("np", "vp", "same")}
PAIR_ORDER = PAIR_IDS
HISTORY_LEN = 20000           # точек на пару (раз в секунду ≈ 5,5 часа)


def shaft_short(sid: str) -> str:
    row, side = SHAFTS[sid]
    return ROW_LETTER[row] + SIDE_LETTER[side] + "В"


def pair_short(pid: str) -> str:
    a, b, _ = PAIRS[pid]
    return shaft_short(a) + "–" + shaft_short(b)


def default_shafts_cfg() -> dict:
    return normalize_shafts(None)


def assign_shafts(cams: list) -> dict:
    """Какая зона-цикл какому валу соответствует: {id вала: (id камеры, id зоны)}.

    Камера с заданным рядом отдаёт свои зоны-циклы (включённые) по порядку слева направо: первая — левый вал, вторая — правый.
    Одна зона на камере — левая, если ось левее середины кадра, иначе правая. Если у двух камер один ряд — берётся первая в конфиге."""
    out: dict = {}
    for c in cams:
        row = c.get("row") or ""
        if row not in ROWS or not c.get("enabled", True):
            continue
        zs = sorted((z for z in c.get("zones", []) if z.get("type") == "cycle" and z.get("enabled", True) is not False),
                    key=lambda z: z["center"][0])
        if not zs:
            continue
        if len(zs) == 1:
            fw = (c.get("frame_size") or [0])[0]
            left = fw <= 0 or zs[0]["center"][0] < fw / 2.0
            picks = [("L" if left else "R", zs[0])]
        else:
            picks = [("L", zs[0]), ("R", zs[1])]
        for side, z in picks:
            sid = next(k for k, v in SHAFTS.items() if v == (row, side))
            out.setdefault(sid, (c["id"], z["id"]))
    return out


def _zone_state(states: dict, cam: str, zone: str) -> Optional[dict]:
    res = (states.get(cam) or {}).get("result") or {}
    if not res.get("alive"):
        return None
    for z in res.get("zones") or []:
        if z.get("id") == zone:
            return z
    return None


def raw_angle(mode: str, a: float, b: float) -> float:
    """Сырое значение угла пары до вычета эталона: сумма (зеркально) или разность (одинаково)."""
    return a + b if mode == "mirror" else a - b


def compute(cfg: dict, states: dict) -> dict:
    """Конфиг сервиса + {id камеры: state()} → снимок валов и пар. Валы без данных — valid False, значения None."""
    sc = cfg.get("shafts") or default_shafts_cfg()
    assign = assign_shafts(cfg.get("cameras", []))
    cam_names = {c["id"]: c.get("name") or c["id"] for c in cfg.get("cameras", [])}
    shafts: dict = {}
    for sid in SHAFT_ORDER:
        cam, zone = assign.get(sid, (None, None))
        zs = _zone_state(states, cam, zone) if cam else None
        ok = bool(zs and zs.get("valid"))
        shafts[sid] = {"id": sid, "short": shaft_short(sid), "camera": cam, "camera_name": cam_names.get(cam), "zone": zone,
                       "assigned": cam is not None, "valid": ok,
                       "angle": round(float(zs["angle"]), 2) if ok else None,
                       "rpm": round(float(zs.get("rpm", 0.0)), 3) if ok else None,
                       "coast": bool(zs.get("coast")) if zs else False}
    pairs: dict = {}
    for pid in PAIR_ORDER:
        a_id, b_id, mode = PAIRS[pid]
        a, b = shafts[a_id], shafts[b_id]
        pc = sc["pairs"][pid]
        ok = a["valid"] and b["valid"]
        raw = round(raw_angle(mode, a["angle"], b["angle"]), 2) if ok else None
        pairs[pid] = {"id": pid, "short": pair_short(pid), "a": a_id, "b": b_id, "mode": mode, "valid": ok,
                      "assigned": a["assigned"] and b["assigned"],
                      "raw": raw,
                      "angle_diff": round(ang_diff(raw, pc["ref"]), 2) if ok else None,
                      "rpm_diff": round(abs(a["rpm"]) - abs(b["rpm"]), 3) if ok else None,
                      "ref": pc["ref"], "tol_angle": pc["tol_angle"], "tol_rpm": pc["tol_rpm"]}
    return {"shafts": shafts, "pairs": pairs}


class ShaftRunner(threading.Thread):
    """Раз в секунду: снимок валов → история (для графика) → тревоги → запись в ПЛК (если задан адрес и ПЛК включён)."""

    def __init__(self, mgr):
        super().__init__(daemon=True, name="shafts")
        self.mgr = mgr
        self._halt = threading.Event()
        self.snap: dict = {"shafts": {}, "pairs": {}}
        self.hist: dict = {p: deque(maxlen=HISTORY_LEN) for p in PAIR_ORDER}
        self._hys: dict = {}              # (пара, "angle"/"rpm") → (допуск, Hysteresis)
        self._sent: dict = {}
        self.status: dict = {}            # ключ вывода → {ok, error}

    def stop(self):
        self._halt.set()

    def snapshot(self) -> dict:
        return self.snap

    def history(self, pair: str, window_s: float = 3600.0, points: int = 600) -> dict:
        rows = list(self.hist.get(pair, ()))
        if window_s > 0:
            t0 = time.time() - window_s
            rows = [r for r in rows if r[0] >= t0]
        if points > 0 and len(rows) > points:
            step = len(rows) / float(points)
            rows = [rows[int(i * step)] for i in range(points)]
        return {"pair": pair, "t": [round(r[0], 1) for r in rows], "angle": [r[1] for r in rows], "rpm": [r[2] for r in rows]}

    def _alarm(self, pid: str, kind: str, tol: float, value: Optional[float]) -> int:
        """Тревога по допуску: tol 0 — выкл; нет данных — сброс (нечего судить)."""
        key = (pid, kind)
        if not tol or value is None:
            self._hys.pop(key, None)
            return 0
        cur = self._hys.get(key)
        if cur is None or cur[0] != tol:
            cur = (tol, Hysteresis(tol, tol * 0.7, False, 3, 5))
            self._hys[key] = cur
        return int(cur[1].update(abs(value)))

    def tick(self, now: Optional[float] = None):
        now = time.time() if now is None else now
        cfg = self.mgr.cfg
        states = {cid: w.state() for cid, w in list(self.mgr.workers.items())}
        snap = compute(cfg, states)
        for pid, p in snap["pairs"].items():
            p["alarm_angle"] = self._alarm(pid, "angle", p["tol_angle"], p["angle_diff"])
            p["alarm_rpm"] = self._alarm(pid, "rpm", p["tol_rpm"], p["rpm_diff"])
            if p["valid"]:
                self.hist[pid].append((now, p["angle_diff"], p["rpm_diff"]))
        self.snap = snap
        mb = cfg["modbus"]
        if not mb.get("enable"):
            return
        sc = cfg.get("shafts") or default_shafts_cfg()
        writes = []             # (ключ, kind, адрес, значение)
        for sid in SHAFT_ORDER:                                  # скорость вала ×100, 0 — нет данных (как у rpm_out зоны)
            out = sc["shafts"][sid].get("out_rpm")
            if out:
                v = snap["shafts"][sid]["rpm"]
                writes.append(("s:" + sid, out["kind"], out["address"], int(round((v or 0.0) * 100))))
        for pid in PAIR_ORDER:
            pc, p = sc["pairs"][pid], snap["pairs"][pid]
            if pc.get("out_angle"):                              # разность углов ×10 (0,1°)
                writes.append(("a:" + pid, pc["out_angle"]["kind"], pc["out_angle"]["address"],
                               int(round((p["angle_diff"] or 0.0) * 10))))
            if pc.get("out_rpm"):                                # разность скоростей ×100
                writes.append(("r:" + pid, pc["out_rpm"]["kind"], pc["out_rpm"]["address"],
                               int(round((p["rpm_diff"] or 0.0) * 100))))
        todo = []
        for key, kind, addr, val in writes:
            val &= 0xFFFF
            prev = self._sent.get(key)
            if prev and prev[0] == (kind, addr, val) and now - prev[1] < mb["write_period_s"]:
                continue
            todo.append((key, (kind, addr, val)))
        if todo:
            link = self.mgr.links.get(mb["host"], mb["port"], mb["device_id"])
            ok = link.write([it for _, it in todo])
            for key, it in todo:
                if ok:
                    self._sent[key] = (it, now)
                self.status[key] = {"ok": ok, "error": "" if ok else link.last_error}
        snap["plc"] = {"enable": True, "outputs": len(writes), "ok": all(v.get("ok") for v in self.status.values()) if self.status else None}

    def run(self):
        while not self._halt.wait(1.0):
            try:
                self.tick()
            except Exception:
                log.exception("валы: сбой расчёта")
