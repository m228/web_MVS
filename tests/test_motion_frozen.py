"""motion_service: камера с застывшей картинкой остаётся мёртвой и после переоткрытия потока — пока не придёт изменившийся кадр."""
import sys
import time
from pathlib import Path

import numpy as np
import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import motion_camera as mcam  # noqa: E402
import motion_engine as me  # noqa: E402
import motion_modbus as mm  # noqa: E402
import motion_scenarios as ms  # noqa: E402
from test_motion_service import H, W, FakeCap, _wait, _worker, make_frames  # noqa: E402
from test_motion_tube import cam, column, tube  # noqa: E402

FROZEN = "картинка застыла"
MUSH = np.full((H, W), 128, np.uint8)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(mcam, "OPEN_GAP_S", 0.0)
    monkeypatch.setattr(mcam, "FREEZE_REOPEN_S", 0.0)      # переоткрытие застывшего потока — через 2·freeze_timeout_s


class Feed:
    """Камера под управлением теста (она же capture_factory): live — кадры идут по кругу, иначе всё время still;
    drop — один раз оборвать поток; head — чем начинается каждое открытие потока в первые head_s секунд."""

    def __init__(self, live=True):
        self.frames = make_frames(40)
        self.live, self.still, self.drop, self.head, self.head_s = live, self.frames[0], False, None, 0.0
        self.opened = self.i = 0

    def __call__(self, url, tr):
        self.opened += 1
        return _FeedCap(self)


class _FeedCap(FakeCap):
    def __init__(self, feed):
        super().__init__(feed.frames)
        self.feed, self.born = feed, time.time()

    def grab(self):
        time.sleep(self.delay)
        f = self.feed
        if f.drop:
            f.drop = False
            return False
        if f.head is not None and time.time() - self.born < f.head_s:
            self._f = f.head
        elif f.live:
            self._f = f.frames[f.i % len(f.frames)]
            f.i += 1
        else:
            self._f = f.still
        return True


def _start(tmp_path, feed, **over):
    kw = dict(url="rtsp://h/cam", reconnect_delay_s=0.5, freeze_timeout_s=1, corrupt_hold_s=0.1)
    kw.update(over)
    return _worker(tmp_path, feed.frames, cap_factory=feed, **kw)


def _watch(w):
    """Все состояния, которые поток камеры объявил с этого момента: [(alive, reason)]."""
    seen, real = [], w._publish

    def publish(state, *a, **kw):
        seen.append((state["alive"], state["reason"]))
        real(state, *a, **kw)
    w._publish = publish
    return seen


def _regs(clients, since=0):
    return [(a, v) for _k, a, v, _d in clients[0].log[since:]] if clients else []


def _result(w):
    return w.state()["result"] or {}


def _dead_frozen(w, clients):
    return _result(w).get("reason") == FROZEN and dict(_regs(clients)).get(9) == 0


def _moving(w, clients):
    res, regs = _result(w), dict(_regs(clients))
    return bool(res.get("alive") and res["zones"][0]["bit"] == 1 and regs.get(0) == 1 and regs.get(9) == 1)


def test_frozen_camera_stays_dead_across_reopens_and_revives_on_changed_frame(tmp_path):
    feed = Feed(live=False)
    w, clients = _start(tmp_path, feed)
    try:
        assert _wait(lambda: _dead_frozen(w, clients))
        seen, mark, opened = _watch(w), len(clients[0].log), feed.opened
        assert _wait(lambda: feed.opened >= opened + 2, 15), "поток переоткрывается раз за разом"
        time.sleep(0.8)
        assert seen and set(seen) == {(False, FROZEN)}, sorted(set(seen))
        assert {v for _a, v in _regs(clients, mark)} == {0}, "в ПЛК всё время нули"
        assert ms.source_bits([{"camera": "w", "zone": "z1"}], {"w": w.state()}) == [0]
        assert [r[2] for r in list(w._history)[-20:]] == [0] * 20, "в графиках и статистике — «нет данных»"
        feed.live = True
        assert _wait(lambda: _moving(w, clients), 10), "пошли новые кадры — камера ожила, биты набрались"
        # застревание забыто: короткая заминка на том же кадре после обрыва — обычный обрыв
        del seen[:]
        feed.live, feed.drop = False, True
        opened = feed.opened
        assert _wait(lambda: feed.opened > opened)
        time.sleep(0.3)
        feed.live = True
        assert _wait(lambda: _moving(w, clients))
        assert {a for a, _r in seen} == {True}, seen
    finally:
        w.stop()


@pytest.mark.parametrize("src", [dict(), dict(url="clip.mp4", loop=False), dict(url="clip.mp4", loop=True)],
                         ids=["rtsp", "file", "file_loop"])
def test_stream_drop_does_not_revive_frozen_camera(tmp_path, monkeypatch, src):
    """Обрыв (у файла — конец) при застывшей картинке: тот же кадр после переоткрытия камеру не оживляет."""
    monkeypatch.setattr(mcam, "FREEZE_REOPEN_S", 1000.0)
    feed = Feed(live=False)
    w, clients = _start(tmp_path, feed, **src)
    try:
        assert _wait(lambda: _dead_frozen(w, clients))
        seen = _watch(w)
        feed.drop = True
        assert _wait(lambda: not feed.drop)
        time.sleep(1.5)
        assert (False, FROZEN) in seen and not any(a for a, _r in seen), sorted(set(seen))
        feed.live = True
        assert _wait(lambda: _moving(w, clients), 10)
    finally:
        w.stop()


@pytest.mark.parametrize("head_s", [0.3, 1.6], ids=["short", "longer_than_timeout"])
def test_grey_mush_after_reopen_does_not_revive_frozen_camera(tmp_path, monkeypatch, head_s):
    """Серая каша — не «изменившийся кадр»: после неё тот же застывший кадр, и камера всё ещё мёртвая."""
    monkeypatch.setattr(mcam, "FREEZE_REOPEN_S", 1000.0)
    feed = Feed(live=False)
    w, clients = _start(tmp_path, feed)
    try:
        assert _wait(lambda: _dead_frozen(w, clients))
        seen, mark = _watch(w), len(clients[0].log)
        feed.head, feed.head_s, feed.drop = MUSH, head_s, True
        assert _wait(lambda: not feed.drop)
        time.sleep(head_s + 1.5)
        assert not any(a for a, _r in seen) and _dead_frozen(w, clients), sorted(set(seen))
        assert all(v == 0 for _a, v in _regs(clients, mark))
        feed.head, feed.live = None, True
        assert _wait(lambda: _moving(w, clients), 10)
    finally:
        w.stop()


def test_grey_mush_inside_frozen_picture_does_not_revive(tmp_path, monkeypatch):
    monkeypatch.setattr(mcam, "FREEZE_REOPEN_S", 1000.0)
    feed = Feed(live=False)
    w, clients = _start(tmp_path, feed)
    try:
        assert _wait(lambda: _dead_frozen(w, clients))
        seen, stuck = _watch(w), feed.still
        feed.still = MUSH
        assert _wait(lambda: _result(w).get("corrupt") is True)
        feed.still = stuck
        time.sleep(0.8)
        assert not any(a for a, _r in seen) and _result(w)["reason"] == FROZEN, sorted(set(seen))
    finally:
        w.stop()


def test_new_resolution_after_reopen_revives(tmp_path, monkeypatch):
    monkeypatch.setattr(mcam, "FREEZE_REOPEN_S", 1000.0)
    feed = Feed(live=False)
    w, clients = _start(tmp_path, feed)
    try:
        assert _wait(lambda: _dead_frozen(w, clients))
        seen = _watch(w)
        feed.still, feed.drop = np.full((H * 2, W * 2), 60, np.uint8), True
        assert _wait(lambda: any(a for a, _r in seen))
    finally:
        w.stop()


def test_short_still_picture_across_drop_keeps_camera_alive(tmp_path):
    """Исправная камера: картинка постояла меньше freeze_timeout_s, обрыв, тот же кадр — это не «застыла»."""
    feed = Feed()
    w, clients = _start(tmp_path, feed, freeze_timeout_s=3)
    try:
        assert _wait(lambda: _moving(w, clients))
        seen = _watch(w)
        feed.live = False
        time.sleep(0.4)
        feed.drop = True
        assert _wait(lambda: feed.opened == 2)
        time.sleep(0.4)
        feed.live = True
        assert _wait(lambda: _moving(w, clients))
        assert {a for a, _r in seen} == {True}, seen
    finally:
        w.stop()


def test_frozen_gauge_keeps_value_for_its_hold_while_camera_stays_dead(tmp_path):
    """Уровнемер: пока идёт «держать», значение есть (с отметкой «держим»), а камера мёртвая и после переоткрытий."""
    feed = Feed(live=False)
    feed.still = np.asarray(column(60))
    feed.frames = [feed.still]

    class Colour(_FeedCap):
        def retrieve(self):
            return True, self._f.copy()

    def factory(url, tr):
        feed.opened += 1
        return Colour(feed)

    cfg = cam(url="rtsp://h/cam", hw_accel="off", proc_fps=20, freeze_timeout_s=1, reconnect_delay_s=0.5, corrupt_ratio=0,
              zones=[tube(hold_s=60)], status_out={"kind": "holding", "address": 9})
    w = mcam.CameraWorker(cfg, me.normalize_modbus({}), mm.LinkRegistry(), tmp_path, factory)
    w.start()
    try:
        assert _wait(lambda: _result(w).get("reason") == FROZEN)
        seen, opened = _watch(w), feed.opened
        assert _wait(lambda: feed.opened >= opened + 2, 15)
        time.sleep(0.8)
        assert set(seen) == {(False, FROZEN)}, sorted(set(seen))
        z = _result(w)["zones"][0]
        assert z["valid"] and z["held"] and z["pct"] == pytest.approx(60, abs=0.6)
        value = ms.source_bits([{"camera": "lv", "zone": "t1"}], {"lv": w.state()}, analog=True, now=time.time())[0]
        assert value == pytest.approx(600, abs=6)
    finally:
        w.stop()
