"""Чёрный кадр микроскопа: определение, счётчик «чёрный подряд», проба пропускается, тревога в sysmon с состоянием платы."""
import numpy as np
import pytest

import camera_params
import sysmon
from test_sysmon import Clock, make_sources, sm  # noqa: F401  (sm — фикстура)


def _frame(value=0, noise=0, shape=(240, 320, 3), seed=1):
    rng = np.random.default_rng(seed)
    img = np.full(shape, value, np.int16)
    if noise:
        img = img + rng.integers(-noise, noise + 1, shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def test_blackness_flat_black_and_dim_noise_are_black():
    assert camera_params.blackness(_frame(0))[0]
    assert camera_params.blackness(_frame(2, noise=2))[0], "слабый шум чёрного поля — всё ещё чёрный кадр"


def test_blackness_live_frames_are_not_black():
    assert not camera_params.blackness(_frame(95, noise=30))[0], "обычный кадр варки (медиана 74–118)"
    assert not camera_params.blackness(_frame(40, noise=10))[0], "тусклый, но живой"
    img = _frame(0)
    img[:, ::7] = 220                                 # тёмное поле с яркими кристаллами: медиана низкая, 99-й процентиль высокий
    assert not camera_params.blackness(img)[0]


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _watch():
    clk = FakeClock()
    return camera_params.BlackWatch(clock=clk), clk


def test_watch_counts_continuous_black_and_resets_on_live_frame():
    w, clk = _watch()
    black, live = _frame(0), _frame(100, noise=30)
    for _ in range(61):                               # 60 с чёрного: кадр раз в секунду
        w.feed(black)
        clk.t += 1
    assert 59 <= w.seconds() <= 61
    w.feed(live)
    assert w.seconds() == 0.0, "один живой кадр обнуляет отсчёт"
    clk.t += 1
    w.feed(black)
    clk.t += 5
    w.feed(black)
    assert 4 <= w.seconds() <= 6


def test_watch_samples_once_per_second_and_ignores_gaps():
    w, clk = _watch()
    black = _frame(0)
    w.feed(black)
    first = w.last
    clk.t += 0.3
    w.feed(_frame(100, noise=30))                     # слишком рано — не проверяется
    assert w.last == first and w.since is not None
    clk.t += 30                                       # поток молчал дольше BLACK_GAP_S: чёрное до паузы не считаем
    assert w.seconds() == 0.0
    w.feed(black)
    assert w.seconds() == 0.0 and w.since == w.last


# ------------------------------------------------------------------------------------------------ тревога sysmon

LED_ON = {"stage": 7, "stage_ok": True, "cyclic": True, "manual": False, "fault": False, "cycle_params": {"trigger_mode": "time"},
          "led_on": True, "led_bright": 60}
EXT = {"uptime_s": 1000, "temp": 41, "u12v": 11900, "init_status": 0x13, "wdt_resets": 2, "lan_resets": 0, "lan_stack_resets": 1}


def _cam(black_s, streaming=True, exposure=40000.0):
    return {"serial": "S1", "streaming": streaming, "current": {"exposure_time": exposure}, "saved": {"exposure_time": exposure},
            "black_s": black_s}


def _agent(sm, **over):
    base = dict(state=lambda: dict(LED_ON), ext=lambda: dict(EXT), camera=lambda: _cam(130.0))
    base.update(over)
    return sm.Agent(sources=make_sources(**base), clock=Clock())


def test_black_alert_after_two_checks_with_plate_details(sm):
    ag = _agent(sm)
    assert ag.tick(1.0) == [], "тревога открывается со второй проверки подряд"
    ev = ag.tick(16.0)
    assert [e["data"]["code"] for e in ev] == ["black"] and ev[0]["level"] == "high"
    t = ev[0]["text"]
    assert "чёрный уже 2 мин" in t and "свет 60 %" in t and "экспозиция 40000" in t
    assert "12V 11.9 В" in t and "0x0013" in t and "сторож 2, LAN 0/1" in t


def test_black_needs_two_minutes(sm):
    ag = _agent(sm, camera=lambda: _cam(90.0))
    assert ag.tick(1.0) == [] and ag.tick(16.0) == []


@pytest.mark.parametrize("state_patch,exposure", [({"led_on": False}, 40000.0), ({"led_bright": 0}, 40000.0), ({}, 0.0)])
def test_black_ignored_when_light_off_or_zero_exposure(sm, state_patch, exposure):
    st = dict(LED_ON, **state_patch)
    ag = _agent(sm, state=lambda: st, camera=lambda: _cam(300.0, exposure=exposure))
    assert ag.tick(1.0) == [] and ag.tick(16.0) == []


def test_black_closes_on_live_frame_and_holds_when_stream_stopped(sm):
    box = {"cam": _cam(130.0)}
    ag = _agent(sm, camera=lambda: box["cam"])
    ag.tick(1.0)
    ag.tick(16.0)
    assert "black" in ag.active
    box["cam"] = _cam(0.0, streaming=False)           # поток стоит — не знаем, вернулся ли свет
    assert ag.tick(31.0) == [] and "black" in ag.active
    box["cam"] = _cam(0.0)                            # поток идёт и кадр живой
    ev = ag.tick(46.0)
    assert [e["data"]["code"] for e in ev] == ["black"] and ev[0]["data"]["state"] == "closed"


def test_probe_black_status_raises_same_alert(sm):
    ag = _agent(sm, camera=lambda: _cam(0.0), cv=lambda: {"state": "black", "message": "x"})
    ag.tick(1.0)
    ev = ag.tick(16.0)
    assert ev and ev[0]["data"]["code"] == "black" and "Проба пропущена" in ev[0]["text"]


def test_plate_unreachable_noted_in_text(sm):
    ag = _agent(sm, status=lambda: {"connected": False})
    ag.tick(1.0)
    ev = [e for e in ag.tick(16.0) if e["data"]["code"] == "black"]
    assert ev and "плата не отвечает" in ev[0]["text"]


# ------------------------------------------------------------------------------------------------------------- проба

def test_probe_skips_black_frame_with_status():
    from microscope_service import micro
    class W:
        current_config = {"exposure_time": 40000.0}
    assert micro._black(_frame(0), W(), "S1") is True
    st = micro.cv_status()
    assert st["state"] == "black" and "чёрный" in st["message"]
    assert micro._black(_frame(100, noise=30), W(), "S1") is False
