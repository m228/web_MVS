"""Автомат пробы (microscope_fsm) без платы и камеры: такт за тактом через fsm.tick().

Плата — фейк: telemetry/status как атрибуты, команды складываются в списки. Камеры и
Modbus нет (см. память про локальную проверку CV/FSM)."""
import copy

import pytest

import plate_config
from microscope_fsm import MicroscopeFSM


class FakePlate:
    """Минимальная плата для FSM: отдаёт телеметрию, принимает команды/позицию в списки."""

    def __init__(self):
        self.telemetry = {"pos1": 0, "pos1_ai": 0, "pos1_enc": 0, "pos2": 0}
        self.status = {"connected": True}
        self.m1_sp = None
        self.m2_sp = None
        self.cmd1_calls = []
        self.stop_calls = []
        self.leds = []
        self.valves = []

    def __post_init__(self):
        pass

    def write_m1_sp(self, v): self.m1_sp = v
    def write_m2_sp(self, v): self.m2_sp = v
    def cmd1(self, v): self.cmd1_calls.append(v)
    def cmd2(self, v): pass
    def motor_goto(self, m, v): pass
    def motor_enable(self, m, on): pass
    def motor_stop(self, m): self.stop_calls.append(m)
    def motor_find_zero(self, m): pass
    def motor_set_zero(self, m): pass
    def motor_steps(self, m, n): pass       # снятие флага «Поиск 0» после автокалибровки (1.9.98)
    def set_led(self, b, on): self.leds.append((b, on))
    def set_valve_tube(self, on): self.valves.append(("tube", on))
    def set_valve_glass(self, on): self.valves.append(("glass", on))


@pytest.fixture
def fsm():
    cfg = copy.deepcopy(plate_config.DEFAULTS)
    # короткие тайминги, чтобы цикл проходил за разумное число тиков
    cfg["probe_cycle"].update({"pause_sec": 1, "trigger_mode": "time", "pre_wash_sec": 0,
                               "post_wash_pause_sec": 0, "dwell_sec": 0, "settle_sec": 0})
    cfg["probe_cycle"]["fast"]["enabled"] = False
    return MicroscopeFSM(FakePlate(), cfg)


def _run_until(fsm, pred, max_ticks=600, track_arrival=True):
    """Крутить tick() до pred() или лимита. track_arrival — телеметрия «доезжает» до текущей
    цели мотора, иначе цикл встанет на доезде. Возвращает множество пройденных mode."""
    seen = set()
    for _ in range(max_ticks):
        if track_arrival and fsm.plate.m1_sp is not None:
            v = fsm.plate.m1_sp
            fsm.plate.telemetry.update({"pos1": v, "pos1_enc": v, "pos1_ai": v})
        fsm.tick()
        seen.add(fsm.mode)
        if pred():
            break
    return seen


def test_auto_mode_writes_motor_setpoint_to_plate(fsm):
    # позитивный контроль: в обычном авто-режиме уставка М1 реально уходит на плату
    fsm.set_cyclic(True)
    fsm.set_stage(7)
    fsm.set_sv(88.0)
    _run_until(fsm, lambda: fsm.plate.m1_sp is not None)
    assert fsm.plate.m1_sp is not None, "write_m1_sp не вызывался — плата не управляется"


def test_full_probe_cycle_runs_to_completion(fsm):
    # цикл проходит 20→21→22→23→24→0, а не застревает на входе
    fsm.set_cyclic(True)
    fsm.set_stage(7)
    fsm.set_sv(88.0)
    # дошли до выдержки (23), затем вернулись в ожидание уже ПОСЛЕ неё
    seen = _run_until(fsm, lambda: fsm.mode == 23)
    assert 20 in seen and 21 in seen and 22 in seen and 23 in seen, "цикл застрял: %s" % sorted(seen)
    seen |= _run_until(fsm, lambda: fsm.mode == 0, max_ticks=400)
    assert 24 in seen, "не было шага возврата (24): %s" % sorted(seen)
    assert fsm.mode == 0, "цикл не вернулся в ожидание, mode=%d" % fsm.mode


def test_estop_stops_both_motors(fsm):
    fsm.set_cyclic(True)
    fsm.set_stage(7)
    fsm.set_movement_inhibit(True)     # SW3 + halt
    fsm.tick()
    assert fsm.plate.stop_calls[:2] == [1, 2], "аварийный стоп не послал motor_stop обоим: %s" % fsm.plate.stop_calls


def test_manual_mode_does_not_write_plate(fsm):
    fsm.set_manual(True)
    fsm.set_stage(7)
    for _ in range(5):
        fsm.tick()
    # в ручном режиме автомат вообще не трогает плату (ранний return)
    assert fsm.plate.m1_sp is None and not fsm.plate.leds


def test_auto_cycle_starts_probe_by_time_trigger(fsm):
    fsm.set_cyclic(True)
    fsm.set_stage(7)              # стадия в рабочей зоне 3..9
    fsm.set_sv(88.0)
    # pause_sec=1 → ~10 тиков в простое, потом mode 20 (отвод)
    started = False
    for _ in range(30):
        fsm.tick()
        if fsm.mode != 0:
            started = True
            break
    assert started, "авто-цикл не стартовал пробу при стадии 7 и включённом цикле"
    assert fsm.mode == 20


def test_no_cycle_outside_stage_range(fsm):
    fsm.set_cyclic(True)
    fsm.set_stage(1)             # вне 3..9 — цикл не стартует
    fsm.set_sv(88.0)
    for _ in range(30):
        fsm.tick()
    assert fsm.mode == 0


def test_stop_movement_inhibits(fsm):
    fsm.set_cyclic(True)
    fsm.set_stage(7)
    fsm.set_movement_inhibit(True)
    for _ in range(30):
        fsm.tick()
    # SW3 (стоп движения) — моторная уставка на плату не пишется
    assert fsm.plate.m1_sp is None


def test_take_sample_from_idle(fsm):
    res = fsm.start_sample()
    assert res["status"] == "started"
    assert fsm.mode == 20


def test_reset_cycle_returns_to_idle(fsm):
    fsm.start_sample()
    fsm.reset_cycle()
    assert fsm.mode == 0
