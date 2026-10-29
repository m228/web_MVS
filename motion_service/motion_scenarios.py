"""Сценарии записи в ПЛК: «зоны разных камер → правило (ИЛИ / И / сумма) → один регистр или coil».

Живут на верхнем уровне конфига (cfg["scenarios"]), считаются отдельным потоком по состояниям камер и пишут в ПЛК
через общий LinkRegistry (тот же канал, что и у камер). Мёртвая камера (нет кадров, фриз, битый поток) даёт 0.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("motion.scenarios")

RULES = ("or", "and", "sum")


def evaluate(rule: str, bits: list) -> int:
    """Биты источников → значение сценария."""
    if rule == "and":
        return 1 if bits and all(bits) else 0
    if rule == "sum":
        return int(sum(1 for b in bits if b))
    return 1 if any(bits) else 0


def source_bits(sources: list, states: dict) -> list:
    """[{camera, zone}] + {id камеры: state()} → биты; камеры/зоны нет или она не жива → 0."""
    out = []
    for s in sources:
        res = (states.get(s["camera"]) or {}).get("result") or {}
        bit = 0
        if res.get("alive"):
            for z in res.get("zones") or []:
                if z["id"] == s["zone"]:
                    bit = int(z["bit"])
        out.append(bit)
    return out


class ScenarioRunner(threading.Thread):
    """Раз в секунду считает сценарии; пишет при изменении значения и не реже write_period_s."""

    def __init__(self, mgr):
        super().__init__(daemon=True, name="scenarios")
        self.mgr = mgr
        self._halt = threading.Event()
        self._sent: dict = {}
        self.status: dict = {}

    def stop(self):
        self._halt.set()

    def snapshot(self) -> list:
        return [dict(v, id=k) for k, v in self.status.items()]

    def tick(self, now: Optional[float] = None):
        now = time.time() if now is None else now
        cfg = self.mgr.cfg
        states = {cid: w.state() for cid, w in list(self.mgr.workers.items())}
        by_link: dict = {}
        live_ids = set()
        for sc in cfg.get("scenarios", []):
            live_ids.add(sc["id"])
            bits = source_bits(sc["sources"], states)
            value = evaluate(sc["rule"], bits)
            mb = sc.get("modbus") or cfg["modbus"]
            st = self.status.setdefault(sc["id"], {"ok": None, "error": ""})
            st.update(name=sc["name"], value=value, bits=bits, plc=bool(mb.get("enable")), out=sc["out"])
            if not mb.get("enable") or not sc["out"]:
                st.update(ok=None, error="")
                continue
            key = (mb["host"], mb["port"], mb["device_id"])
            prev = self._sent.get(sc["id"])
            if prev and prev[0] == value and now - prev[1] < mb["write_period_s"]:
                continue
            by_link.setdefault(key, []).append((sc, (sc["out"]["kind"], sc["out"]["address"], value)))
        for key, items in by_link.items():
            link = self.mgr.links.get(*key)
            ok = link.write([it for _, it in items])
            for sc, it in items:
                if ok:
                    self._sent[sc["id"]] = (it[2], now)
                self.status[sc["id"]].update(ok=ok, error="" if ok else link.last_error)
        for sid in list(self.status):
            if sid not in live_ids:
                self.status.pop(sid, None)
                self._sent.pop(sid, None)

    def run(self):
        while not self._halt.wait(1.0):
            try:
                self.tick()
            except Exception:
                log.exception("сценарии: сбой расчёта")
