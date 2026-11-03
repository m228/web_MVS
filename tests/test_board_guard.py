"""Перезапуск платы при чёрном кадре: когда можно, одна попытка, ожидание возврата, тревога в sysmon."""
import numpy as np
import pytest

import board_guard
import camera_params
import sysmon
from microscope_plc import BOARD_CMD_REG, BOARD_REBOOT, PlateClient
from test_black_frame import FakeClock, _frame
from test_sysmon import make_sources, sm  # noqa: F401  (sm — фикстура)


class FakeBoard:
    """Плата: после команды пропадает на down_s секунд (None — не принимает команду; inf — не вернётся)."""

    def __init__(self, clk, down_s=5.0, delay_s=1.0):
        self.clk, self.down_s, self.delay_s = clk, down_s, delay_s
        self.sent_at = None
        self.sent = 0
        self.up0 = 1000.0

    def reboot(self):
        self.sent += 1
        self.sent_at = self.clk.t

    def _is_up(self):
        if self.sent_at is None or self.down_s is None:
            return True
        dt = self.clk.t - self.sent_at
        return dt < self.delay_s or dt >= self.delay_s + self.down_s

    def status(self):
        up = self._is_up()
        return {"connected": up, "last_ok": self.clk.t if up else (self.clk.t - 3)}

    def ext(self):
        if self.sent_at is not None and self.down_s is not None and self.clk.t - self.sent_at >= self.delay_s + self.down_s:
            return {"uptime_s": self.clk.t - self.sent_at - self.delay_s - self.down_s}      # после перезапуска аптайм начался заново
        return {"uptime_s": self.up0}


def make_guard(board=None, black_s=130.0, mode=0, led_on=True, bright=40, exposure=40000.0, streaming=True, live=False,
               autocal=False):
    clk = FakeClock()
    board = board or FakeBoard(clk)
    board.clk = clk
    cam = {"serial": "S1", "streaming": streaming, "current": {"exposure_time": exposure}, "black_s": black_s, "live": live}
    st = {"mode": mode, "led_on": led_on, "led_bright": bright, "autocal": {"active": autocal}}

    def sleep(s):
        clk.t += s

    src = {"camera": lambda: dict(cam), "state": lambda: dict(st), "status": board.status, "ext": board.ext, "reboot": board.reboot}
    g = board_guard.BoardGuard(src, clock=clk, sleep=sleep)
    g.cam, g.st, g.board, g.clk = cam, st, board, clk
    return g


def test_reboots_once_when_black_and_board_returns():
    g = make_guard()
    assert g.tick() == "returned"
    assert g.board.sent == 1
    assert g.last["result"] == "returned" and 5 <= g.last["took_s"] <= 12
    assert g.tick() is None and g.board.sent == 1, "кадр всё ещё чёрный — второй раз плату не трогаем"


@pytest.mark.parametrize("kw", [
    {"black_s": 100.0},                     # чёрный меньше двух минут
    {"led_on": False}, {"bright": 0},       # свет не горит — чернота ожидаема
    {"exposure": 0.0},                      # экспозиции нет
    {"streaming": False},                   # поток молчит
    {"mode": 22},                           # идёт подвод к стеклу
    {"mode": 23},                           # идёт проба
    {"autocal": True},                      # калибровка нуля
])
def test_no_reboot_when_not_needed_or_not_safe(kw):
    g = make_guard(**kw)
    assert g.tick() is None
    assert g.board.sent == 0


def test_no_reboot_when_board_already_down():
    g = make_guard()
    g.board.down_s, g.board.sent_at = float("inf"), g.clk.t - 100       # плата уже лежит
    g.board.sent = 0
    assert g.tick() is None and g.board.sent == 0


def test_waits_for_safe_step_then_reboots():
    g = make_guard(mode=23)
    assert g.tick() is None
    g.st["mode"] = 0                                                    # проба кончилась
    assert g.tick() == "returned" and g.board.sent == 1


def test_rearmed_by_live_frame_but_cooldown_holds():
    g = make_guard()
    g.tick()
    g.cam["live"] = True
    g.tick()
    g.cam["live"] = False
    g.clk.t += 600                                                      # через 10 мин снова чёрный
    assert g.tick() is None and g.board.sent == 1, "перезапускали недавно"
    g.clk.t += board_guard.COOLDOWN_S
    assert g.tick() == "returned" and g.board.sent == 2


def test_board_never_returns():
    g = make_guard(FakeBoard(FakeClock(), down_s=float("inf")))
    assert g.tick() == "no_return"
    assert g.last["result"] == "no_return"
    assert g.board.sent == 1
    assert g.tick() is None and g.board.sent == 1


def test_board_ignores_command():
    g = make_guard(FakeBoard(FakeClock(), down_s=None))
    assert g.tick() == "no_reboot"
    assert "не перезапустилась" in g.last["text"]


def test_late_return_clears_no_return():
    g = make_guard(FakeBoard(FakeClock(), down_s=60.0))
    assert g.tick() == "no_return"
    g.clk.t += 30                                                       # плата подняла связь позже
    g.tick()
    assert g.last["result"] == "late_return"


def test_uptime_reset_counts_as_return():
    g = make_guard()
    assert g._wait_back(g.clk.t, 5000.0) == ("returned", 0)             # аптайм упал с 5000 до 1000+, плата отвечает


def test_plate_client_reboot_writes_only_the_reboot_constant():
    pc = PlateClient({"unit": 1, "read_base": 1270, "write_base": 1250, "block_len": 10, "poll_interval_ms": 100,
                      "host": "h", "port": 502, "out": {}})
    pc.reboot()
    assert pc._pending == [(BOARD_CMD_REG, BOARD_REBOOT)]
    assert (BOARD_CMD_REG, BOARD_REBOOT) == (1570, 0xAAAA)


def test_black_watch_live():
    clk = FakeClock()
    w = camera_params.BlackWatch(clock=clk)
    assert not w.live()
    w.feed(_frame(100, noise=30))
    assert w.live()
    clk.t += 5
    w.feed(_frame(0))
    assert not w.live(), "последний кадр чёрный"
    clk.t += 5
    w.feed(_frame(100, noise=30))
    clk.t += 30
    assert not w.live(), "поток замолчал"


# ------------------------------------------------------------------------------------------------- sysmon

def _agent(sm, guard=None, **over):
    from test_black_frame import EXT, LED_ON, _cam
    base = dict(state=lambda: dict(LED_ON), ext=lambda: dict(EXT), camera=lambda: _cam(130.0), guard=lambda: guard or {})
    base.update(over)
    return sm.Agent(sources=make_sources(**base), clock=lambda: 1_000_050.0)


def test_sysmon_alert_when_board_did_not_return_is_immediate(sm):
    ag = _agent(sm, guard={"t": 1_000_000.0, "result": "no_return", "text": "плата не поднялась за 45 с"})
    ev = [e for e in ag.tick(1.0) if e["data"].get("code") == "board_reboot"]
    assert ev and ev[0]["level"] == "high", "тревога с первой проверки, без подтверждений"
    assert "не поднялась" in ev[0]["text"]
    assert ag.tick(16.0) is not None
    ag.src["guard"] = lambda: {"t": 1_000_000.0, "result": "late_return", "text": "плата вернулась только позже"}
    closed = [e for e in ag.tick(31.0) if e["data"].get("code") == "board_reboot"]
    assert closed and closed[0]["data"]["state"] == "closed"


def test_sysmon_black_text_mentions_reboot(sm):
    ag = _agent(sm, guard={"t": 1_000_000.0, "result": "returned", "text": "плата вернулась через 6 с"})
    ag.tick(1.0)
    black = [e for e in ag.tick(16.0) if e["data"].get("code") == "black"]
    assert black and "плату уже перезапускали" in black[0]["text"] and "вернулась через 6 с" in black[0]["text"]


def test_sysmon_plate_reboot_event_notes_guard(sm):
    from test_black_frame import _cam
    box = {"up": 5000}
    ag = _agent(sm, guard={"t": 1_000_040.0, "result": "returned", "text": "x"}, ext=lambda: {"uptime_s": box["up"]},
                camera=lambda: _cam(0.0))
    ag.tick(1.0)
    box["up"] = 3
    rb = [e for e in ag.tick(16.0) if e["kind"] == "plate_reboot"]
    assert rb and "защиты от чёрного кадра" in rb[0]["text"]
