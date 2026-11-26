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

RULES = ("or", "and", "sum", "value")
SIGNALS = ("move", "pulp")      # move — бит «движется» зоны, pulp — «есть» (вид окна; без включённого вида окна — 0).
#                                 У зоны без движения (motion=False) бит зоны и есть «есть»: оба сигнала дают одно и то же
NO_DATA = -1                    # value (уровнемер): значения нет — не 0, потому что 0 — это настоящие «0 %». В регистре −1 = 65535: в ПЛК читать как INT


def evaluate(rule: str, bits: list) -> int:
    """Значения источников → значение сценария: or — хоть одно > 0, and — все > 0, sum — сумма значений (бит зоны 0/1, у группы — её значение),
    value — значение единственного источника как есть (уровнемер: % × 10 или целые %, нет данных — −1)."""
    if rule == "value":
        return int(bits[0]) if bits else NO_DATA
    if rule == "and":
        return 1 if bits and all(bits) else 0
    if rule == "sum":
        return int(sum(bits))
    return 1 if any(bits) else 0


def source_bits(sources: list, states: dict, signal: str = "move", analog: bool = False, scale: int = 10, now: Optional[float] = None) -> list:
    """[{camera, zone | group}] + {id камеры: state()} → значения; камеры/зоны/группы нет или она не жива → 0. Зона: signal move —
    бит движения, pulp — «жом есть» (z["fill"] == 1; у зоны без вида окна или без порогов решения нет → 0). Группа: её значение.
    analog (правило value, уровнемер): проценты зоны × scale (10 — 54,3 % → 543, 1 — 54), половина округляется вверх. Камера может быть и не
    жива: пока не вышло «держать», уровнемер отдаёт последнее значение (valid). Значения нет, нет камеры или состояние устарело (now позже
    until: поток камеры завис и состояние не обновляется) → −1 при любом scale."""
    out = []
    for s in sources:
        res = (states.get(s["camera"]) or {}).get("result") or {}
        bit = NO_DATA if analog else 0
        if analog:
            z = next((q for q in res.get("zones") or [] if q["id"] == s.get("zone")), None)
            if z and z.get("valid") and not (now is not None and z.get("until") is not None and now > z["until"]):
                bit = int(z["pct"] * scale + 0.5)     # не round(): тот даёт 54,5 → 54
        elif res.get("alive") and s.get("group") is not None:
            for g in res.get("groups") or []:
                if g["id"] == s["group"]:
                    bit = int(g["value"])
        elif res.get("alive"):
            for z in res.get("zones") or []:
                if z["id"] == s["zone"]:
                    bit = int(z.get("fill") == 1) if signal == "pulp" else int(z["bit"])
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
            scale = sc.get("scale", 10)
            bits = source_bits(sc["sources"], states, sc.get("signal", "move"), sc["rule"] == "value", scale, now)
            value = evaluate(sc["rule"], bits)
            mb = sc.get("modbus") or cfg["modbus"]
            st = self.status.setdefault(sc["id"], {"ok": None, "error": ""})
            # scale — в статусе рядом с value: интерфейс переводит число в проценты тем форматом, которым оно посчитано
            st.update(name=sc["name"], value=value, bits=bits, plc=bool(mb.get("enable")), out=sc["out"], signal=sc.get("signal", "move"), scale=scale)
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
