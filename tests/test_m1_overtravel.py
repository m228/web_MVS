"""Защита М1 от убегания: любой из трёх датчиков выше m1_overtravel_um -> «Стоп М1» + тревога high, без плана платы."""
import copy

import pytest

import microscope_fsm
import plate_config
import sysmon
from microscope_fsm import MicroscopeFSM
from test_fsm import FakePlate


class Plate(FakePlate):
    def __init__(self):
        super().__init__()
        self.steps_calls = []

    def motor_steps(self, m, n):
        self.steps_calls.append((m, n))


@pytest.fixture
def fsm(monkeypatch):
    sent = []
    monkeypatch.setattr(sysmon, "report", lambda kind, level, text, data=None: sent.append((kind, level, text, data)) or True)
    f = MicroscopeFSM(Plate(), copy.deepcopy(plate_config.DEFAULTS))
    f.sent = sent
    return f


def _tick(f, calc=0, enc=0, ai=0, n=1):
    # аналоговый датчик в телеметрии — «сырой», автомат умножает его на sensor_scale (по умолчанию 0.1)
    f.plate.telemetry.update({"pos1": calc, "pos1_enc": enc, "pos1_ai": ai / f._sensor_scale})
    for _ in range(n):
        f.tick()


def test_defaults_are_small():
    d = plate_config.DEFAULTS
    assert d["m1_overtravel_um"] == 9000
    assert d["probe_cycle"]["retract_pos"] <= 5000 and d["SP"][0] <= 5000


def test_below_limit_is_quiet(fsm):
    _tick(fsm, 5000, 5000, 5000, n=30)
    assert fsm.plate.stop_calls == [] and fsm.sent == []


@pytest.mark.parametrize("which", ["calc", "enc", "ai"])
def test_any_sensor_over_limit_stops_and_alerts(fsm, which):
    vals = {"calc": 3000, "enc": 3000, "ai": 3000}
    vals[which] = 9500
    _tick(fsm, vals["calc"], vals["enc"], vals["ai"], n=2)
    assert fsm.plate.stop_calls[0] == 1          # дальше Авария сама стопит оба мотора (sw3)
    assert fsm.plate.steps_calls == [(1, 0)]
    assert len(fsm.sent) == 1
    kind, level, text, data = fsm.sent[0]
    assert (kind, level) == ("m1_overtravel", "high") and "9000" in text and data[which] >= 9500
    assert fsm.mode_state() == "fault"           # автомат остановлен, повторный goto не уйдёт


def test_single_noisy_tick_ignored(fsm):
    _tick(fsm, 9100, 9100, 9100, n=1)
    _tick(fsm, 4000, 4000, 4000, n=1)
    assert fsm.plate.stop_calls == [] and fsm.sent == []


def test_stop_repeats_while_over_but_alert_once(fsm):
    _tick(fsm, 12000, 12000, 9650, n=40)
    m1 = fsm.plate.stop_calls.count(1)
    assert m1 > 3                                 # стоп повторяется, пока выше порога
    assert m1 <= microscope_fsm.OT_STOP_MAX + 100  # Авария тоже стопит М1 каждый такт — верхняя граница грубая
    assert len(fsm.sent) == 1                     # тревога не заливает Telegram


def test_realert_after_interval_and_rearm_after_return(fsm, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(microscope_fsm.time, "monotonic", lambda: t[0])
    _tick(fsm, 10000, 10000, 9650, n=3)
    assert len(fsm.sent) == 1
    t[0] += microscope_fsm.OT_REALERT_SEC + 1
    _tick(fsm, 10000, 10000, 9650, n=1)
    assert len(fsm.sent) == 2 and "всё ещё" in fsm.sent[1][2]
    _tick(fsm, 4000, 4000, 4000, n=3)             # вернулся ниже порога − 500
    _tick(fsm, 9800, 9800, 9800, n=2)
    assert len(fsm.sent) == 3 and "всё ещё" not in fsm.sent[2][2]


def test_manual_mode_stops_and_alerts_without_fault(fsm):
    fsm.set_manual(True)
    _tick(fsm, 10000, 10000, 9650, n=2)
    assert fsm.plate.stop_calls == [1] and len(fsm.sent) == 1
    assert fsm.mode_state() == "manual"           # оператору не блокируем пульт


def test_disabled_with_zero(fsm):
    fsm._ot_limit = 0
    _tick(fsm, 20000, 20000, 9650, n=5)
    assert fsm.plate.stop_calls == [] and fsm.sent == []


def test_missing_values_do_not_crash(fsm):
    fsm.plate.telemetry.update({"pos1": None, "pos1_enc": None, "pos1_ai": None})
    fsm.tick()
    assert fsm.plate.stop_calls == []
