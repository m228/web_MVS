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
        self.leds = []
        self.valves = []

    def write_m1_sp(self, v): self.m1_sp = v
    def write_m2_sp(self, v): self.m2_sp = v
    def cmd1(self, v): self.cmd1_calls.append(v)
    def cmd2(self, v): pass
    def motor_goto(self, m, v): pass
    def motor_enable(self, m, on): pass
    def motor_stop(self, m): pass
    def motor_find_zero(self, m): pass
    def motor_set_zero(self, m): pass
    def set_led(self, b, on): self.leds.append((b, on))
    def set_valve_tube(self, on): self.valves.append(("tube", on))
    def set_valve_glass(self, on): self.valves.append(("glass", on))


@pytest.fixture
def fsm():
    cfg = copy.deepcopy(plate_config.DEFAULTS)
    cfg["probe_cycle"]["pause_sec"] = 1          # быстро для теста
    cfg["probe_cycle"]["trigger_mode"] = "time"
    return MicroscopeFSM(FakePlate(), cfg)


def test_ticks_accept_sv_stage(fsm):
    fsm.set_sv(87.5)
    fsm.set_stage(7)
    fsm.tick()
    assert fsm.sv == 87.5 and fsm.stage == 7


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
