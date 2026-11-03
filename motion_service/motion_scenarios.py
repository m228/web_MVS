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

RULES = ("or", "and", "sum", "level", "each")
SIGNALS = ("move", "pulp", "state")      # move — бит «движется» зоны, pulp — «жом есть» (вид окна; без включённого вида окна — 0), state — состояние окна 0/1/2


def evaluate(rule: str, bits: list) -> int:
    """Значения источников → значение сценария: or — хоть одно > 0, and — все > 0, sum — сумма значений (бит зоны 0/1, у группы — её значение),
    level — уровень снизу вверх: номер самого верхнего активного источника (источники идут снизу вверх; всё ниже него считается заполненным),
    each — для показа сумма (в ПЛК каждый источник пишется отдельно, см. writes_for)."""
    if rule == "and":
        return 1 if bits and all(bits) else 0
    if rule == "sum" or rule == "each":
        return int(sum(bits))
    if rule == "level":
        return max((i for i, b in enumerate(bits, 1) if b), default=0)
    return 1 if any(bits) else 0


def writes_for(sc: dict, bits: list, value: int) -> list:
    """Что писать в ПЛК: [(kind, адрес, значение)]. Обычно одно значение; правило each — по регистру на источник: адрес, адрес+1, …"""
    out = sc["out"]
    if sc["rule"] == "each":
        return [(out["kind"], out["address"] + i, int(b)) for i, b in enumerate(bits) if out["address"] + i <= 65535]
    return [(out["kind"], out["address"], value)]


def source_bits(sources: list, states: dict, signal: str = "move") -> list:
    """[{camera, zone | group}] + {id камеры: state()} → значения; камеры/зоны/группы нет или она не жива → 0. Зона: signal move —
    бит движения, pulp — «жом есть» (z["fill"] == 1; у зоны без вида окна или без порогов решения нет → 0), state — состояние окна
    0 пусто / 1 жом стоит / 2 движется (без порогов вида окна — бит движения). Группа: её значение."""
    out = []
    for s in sources:
        res = (states.get(s["camera"]) or {}).get("result") or {}
        bit = 0
        if res.get("alive") and s.get("group") is not None:
            for g in res.get("groups") or []:
                if g["id"] == s["group"]:
                    bit = int(g["value"])
        elif res.get("alive"):
            for z in res.get("zones") or []:
                if z["id"] == s["zone"]:
                    if signal == "pulp":
                        bit = int(z.get("fill") == 1)
                    elif signal == "state" and z.get("state") is not None:
                        bit = int(z["state"])
                    else:
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
            bits = source_bits(sc["sources"], states, sc.get("signal", "move"))
            value = evaluate(sc["rule"], bits)
            mb = sc.get("modbus") or cfg["modbus"]
            st = self.status.setdefault(sc["id"], {"ok": None, "error": ""})
            st.update(name=sc["name"], value=value, bits=bits, plc=bool(mb.get("enable")), out=sc["out"], signal=sc.get("signal", "move"))
            if not mb.get("enable") or not sc["out"]:
                st.update(ok=None, error="")
                continue
            key = (mb["host"], mb["port"], mb["device_id"])
            writes = writes_for(sc, bits, value)
            prev = self._sent.get(sc["id"])
            if prev and prev[0] == writes and now - prev[1] < mb["write_period_s"]:
                continue
            by_link.setdefault(key, []).append((sc, writes))
        for key, items in by_link.items():
            link = self.mgr.links.get(*key)
            ok = link.write([w for _, ws in items for w in ws])
            for sc, ws in items:
                if ok:
                    self._sent[sc["id"]] = (ws, now)
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
