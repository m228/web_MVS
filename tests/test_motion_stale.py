"""motion_service: сбой разбора кадра не убивает камеру, а замолчавший поток камеры не остаётся «живым» со старыми значениями."""
import logging
import sys
import threading
import time
from pathlib import Path

import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import motion_camera as mcam  # noqa: E402
import motion_engine as me  # noqa: E402
import motion_modbus as mm  # noqa: E402
import motion_scenarios as ms  # noqa: E402
from test_motion_service import FakeCap, FakeClientNew, _wait, _worker, make_frames  # noqa: E402
from test_motion_tube import cam, column, run, tube  # noqa: E402


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(mcam, "OPEN_GAP_S", 0.0)
    monkeypatch.setattr(mcam, "STALE_MIN_S", 0.5)


class Fault:
    """Подмена engine.process: mode ok — работает, raise — бросает, hang — висит до release()."""

    def __init__(self, w):
        self.real, self.mode, self.left, self.calls = w.engine.process, "ok", 0, 0
        self.gate = threading.Event()
        w.engine.process = self

    def __call__(self, *a, **kw):
        self.calls += 1
        if self.mode == "hang":
            self.gate.wait(20)
        if self.mode == "raise" or self.left > 0:
            self.left -= 1
            raise RuntimeError("Insufficient memory")
        return self.real(*a, **kw)


def _regs(clients, since=0):
    """Последние значения регистров ПЛК из записей после первых since."""
    return {a: v for _k, a, v, _d in clients[0].log[since:]} if clients else {}


def _scenario(w, signal="move"):
    return ms.source_bits([{"camera": "w", "zone": "z1"}], {"w": w.state()}, signal)


def _moving(w, clients):
    res = w.state()["result"]
    return bool(res and res["alive"] and res["zones"][0]["bit"] == 1 and _regs(clients).get(0) == 1 and _regs(clients).get(9) == 1)


def test_single_frame_failure_is_skipped_and_logged_once(tmp_path, caplog):
    w, clients = _worker(tmp_path, make_frames(40))
    try:
        fault = Fault(w)
        assert _wait(lambda: _moving(w, clients))
        with caplog.at_level(logging.ERROR, logger="motion.camera"):
            fault.left = 4
            seen = fault.calls
            assert _wait(lambda: fault.calls > seen + 10)
        assert w.is_alive() and _moving(w, clients), "короткий сбой: бит и status держатся"
        errors = [r for r in caplog.records if "сбой анализа кадра" in r.getMessage()]
        assert len(errors) == 1 and errors[0].exc_info, "в лог — один раз, с трассировкой"
    finally:
        w.stop()


def test_lasting_failure_makes_camera_dead_then_recovers(tmp_path):
    w, clients = _worker(tmp_path, make_frames(40), url="rtsp://h/cam", reconnect_delay_s=0.5)
    try:
        fault = Fault(w)
        assert _wait(lambda: _moving(w, clients))
        mark = len(clients[0].log)
        fault.mode = "raise"
        assert _wait(lambda: _regs(clients, mark).get(9) == 0)
        assert _regs(clients, mark)[0] == 0
        assert _wait(lambda: w.state()["result"]["reason"] == mcam.FAIL_REASON)     # поток камеры сам объявил сбой
        st = w.state()
        assert not st["result"]["alive"] and [z["bit"] for z in st["result"]["zones"]] == [0, 0]
        assert st["result"]["reason"] == st["error"] == mcam.FAIL_REASON
        assert _scenario(w) == [0] and w.shot()["info"]["alive"] is False
        calls = fault.calls
        assert _wait(lambda: fault.calls > calls), "анализ продолжает пробовать"
        fault.mode = "ok"
        assert _wait(lambda: _moving(w, clients)), "камера ожила сама"
        assert w.state()["error"] == ""
    finally:
        w.stop()


def test_failure_from_first_frame_still_reports_dead(tmp_path):
    cfg_frames = make_frames(40)
    w, clients = _worker(tmp_path, cfg_frames, cap_factory=lambda u, t: FakeCap(cfg_frames, delay=0.2))
    fault = Fault(w)
    fault.mode = "raise"
    try:
        assert _wait(lambda: _regs(clients).get(9) == 0)
        assert w.is_alive() and not w.state()["result"]["alive"]
        assert all(v == 0 for _k, _a, v, _d in clients[0].log)
    finally:
        w.stop()


def test_exception_outside_engine_does_not_kill_thread(tmp_path):
    frames = make_frames(40)
    broken = [True]

    class BadGrab(FakeCap):
        def grab(self):
            if broken:
                broken.clear()
                raise RuntimeError("grab")
            return super().grab()

    w, clients = _worker(tmp_path, frames, cap_factory=lambda u, t: BadGrab(frames))
    try:
        assert _wait(lambda: _moving(w, clients)) and w.is_alive()
    finally:
        w.stop()


def test_hung_thread_goes_dead_everywhere(tmp_path):
    w, clients = _worker(tmp_path, make_frames(40))
    fault = Fault(w)
    try:
        assert _wait(lambda: _moving(w, clients))
        mark = len(clients[0].log)
        fault.mode = "hang"
        assert _wait(lambda: _regs(clients, mark).get(9) == 0), "поток ПЛК сам пишет «камера не жива»"
        assert _regs(clients, mark)[0] == 0
        st = w.state()
        assert not st["result"]["alive"] and [z["bit"] for z in st["result"]["zones"]] == [0, 0] and st["fps"] == 0
        assert st["error"] == mcam.STALE_REASON and _scenario(w) == [0]
        fault.mode = "ok"
        fault.gate.set()
        assert _wait(lambda: _moving(w, clients))
    finally:
        fault.gate.set()
        w.stop()


def test_stale_threshold_follows_freeze_timeout_and_frame_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(mcam, "STALE_MIN_S", 5.0)

    def after(**kw):
        raw = {"id": "c", "url": "x", "zones": []}
        raw.update(kw)
        return mcam.CameraWorker(me.normalize_camera(raw), me.normalize_modbus({}), mm.LinkRegistry(lambda h, p: None), tmp_path)._stale_after()

    assert after() == 10 and after(freeze_timeout_s=1) == 5 and after(freeze_timeout_s=60) == 60
    assert after(proc_fps=0.2, frame_gap=3) == pytest.approx(25), "редкие кадры не выглядят пропажей"


def test_silent_thread_keeps_gauge_for_its_hold_only(tmp_path):
    """Уровнемер у замолчавшей камеры: «держать» не вышло — значение и бит есть, вышло — нет данных (и в ПЛК, и в сценарии)."""
    c = cam(zones=[tube(hold_s=60)], status_out={"kind": "holding", "address": 9})
    w = mcam.CameraWorker(c, me.normalize_modbus({}), mm.LinkRegistry(lambda h, p: None), tmp_path)
    now = time.time()
    value = lambda t: ms.source_bits([{"camera": "lv", "zone": "t1"}], {"lv": w.state()}, analog=True, now=t)[0]
    w._publish(run(w.engine, column(40), now - 1.0, 3), None, False)
    assert value(now) == pytest.approx(400, abs=6)

    w._pub_mono -= 3600                                     # поток камеры давно молчит
    st, writes, stale = w._current()
    assert stale and not st["alive"] and st["zones"][0]["held"] and writes == [("holding", 9, 0)]
    assert value(now) == pytest.approx(400, abs=6), "«держать» ещё идёт"

    c["zones"][0]["hold_s"] = 0.0                            # «держать» вышло
    assert not w._current()[0]["zones"][0]["valid"]
    assert value(now) == ms.NO_DATA, "сразу, не дожидаясь until последнего живого состояния"
    assert w.engine.last_state["alive"], "чужой поток движок не трогает"
