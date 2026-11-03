"""Декодирование RTSP на видеоядре: режим из plate_config, откат на процессор, учёт реального декодера."""
import time

import cv2
import numpy as np
import pytest

from camera_core import hw_decode
from camera_core import rtsp_worker as rw
from camera_core.rtsp_worker import RtspCameraWorker


@pytest.fixture(autouse=True)
def _no_open_gap(monkeypatch):
    """Очередь на открытие потоков в тестах не ждём (проверяется отдельным тестом ниже)."""
    monkeypatch.setattr(hw_decode, "OPEN_GAP_S", 0.0)


def test_open_turn_spaces_openings_without_holding_the_slow_one():
    """Три камеры открываются с шагом gap: ждёт только тот, кому рано; следующий в очереди не ждёт окончания чужого открытия."""
    hw_decode._open_next = 0.0
    t = [100.0]
    slept = []

    def sleep(s):
        slept.append(round(s, 3))
        t[0] += s
    waits = [hw_decode.wait_open_turn(2.0, clock=lambda: t[0], sleep=sleep) for _ in range(3)]
    assert waits == [0.0, 2.0, 2.0] and slept == [2.0, 2.0]
    t[0] += 30                                         # долго молчали — следующий открывается сразу
    assert hw_decode.wait_open_turn(2.0, clock=lambda: t[0], sleep=sleep) == 0.0


class Cap:
    def __init__(self, opened=True, accel=cv2.VIDEO_ACCELERATION_D3D11):
        self.opened, self.accel = opened, accel

    def isOpened(self):
        return self.opened

    def get(self, prop):
        return self.accel if prop == cv2.CAP_PROP_HW_ACCELERATION else 0

    def set(self, *a):
        return True

    def release(self):
        pass


def _worker(monkeypatch, mode, factory):
    monkeypatch.setattr(rw.plate_config, "load", lambda: {"rtsp": {"hw_accel": mode}} if mode else {})
    calls = []

    def fake(url, api, params=None):
        hw = bool(params)
        calls.append("hw" if hw else "cpu")
        return factory(hw)
    monkeypatch.setattr(rw.cv2, "VideoCapture", fake)
    return RtspCameraWorker("t_hw", None, "rtsp://example/x"), calls


def test_normalize_mode():
    assert hw_decode.normalize_mode(None) == "auto"
    assert hw_decode.normalize_mode("D3D11") == "d3d11"
    assert hw_decode.normalize_mode("cuda") == "auto"
    assert hw_decode.normalize_mode("off") == "off"


def test_default_is_auto_and_reports_gpu(monkeypatch):
    w, calls = _worker(monkeypatch, None, lambda hw: Cap(True))
    assert w._open_capture().isOpened()
    assert calls == ["hw"] and w.decoder == "gpu" and w.stream_state()["decoder"] == "gpu"


def test_auto_silently_on_cpu_is_reported_as_cpu(monkeypatch):
    # драйвер принял запрос, но декодирует процессор (так на ПК с RTX без поддержки кодека)
    w, calls = _worker(monkeypatch, "auto", lambda hw: Cap(True, accel=0))
    assert w._open_capture().isOpened() and w.decoder == "cpu" and not w._hw_off


def test_off_never_asks_for_hardware(monkeypatch):
    w, calls = _worker(monkeypatch, "off", lambda hw: Cap(True, accel=0))
    assert w._open_capture().isOpened()
    assert calls == ["cpu"] and w.decoder == "cpu"


def test_falls_back_to_cpu_when_hardware_open_fails(monkeypatch):
    w, calls = _worker(monkeypatch, "auto", lambda hw: Cap(not hw, accel=0))
    assert w._open_capture().isOpened() and w._hw_off and calls == ["hw", "cpu"]
    calls.clear()
    assert w._open_capture().isOpened() and calls == ["cpu"], "после отката видеоядро больше не просим"


def test_dead_camera_does_not_disable_hardware_and_retries_cpu_rarely(monkeypatch):
    w, calls = _worker(monkeypatch, "auto", lambda hw: Cap(False))
    for _ in range(4):
        assert not w._open_capture().isOpened()
    assert not w._hw_off
    assert calls == ["hw", "cpu", "hw", "hw", "hw", "cpu"], "процессорная попытка — на 1-м и 4-м провале, не каждый раз"


def test_gives_up_hardware_after_three_opens_without_frames(monkeypatch):
    w, _ = _worker(monkeypatch, "auto", lambda hw: Cap(True))
    w._open_capture()
    w._hw_giveup_check(False)
    w._hw_giveup_check(False)
    assert not w._hw_off
    w._hw_giveup_check(True)               # кадры были: счётчик сброшен
    w._hw_giveup_check(False)
    w._hw_giveup_check(False)
    assert not w._hw_off
    w._hw_giveup_check(False)
    assert w._hw_off


class CountingCap:
    """Камера 25 к/с: считает, сколько кадров реально переведено в картинку (read) и сколько только вычитано (grab)."""
    def __init__(self):
        self.reads = self.grabs = 0

    def isOpened(self):
        return True

    def get(self, prop):
        return 25.0 if prop == cv2.CAP_PROP_FPS else 0

    def set(self, *a):
        return True

    def grab(self):
        time.sleep(0.01)
        self.grabs += 1
        return True

    def read(self):
        time.sleep(0.01)
        self.reads += 1
        return True, np.zeros((48, 64, 3), np.uint8)

    def release(self):
        pass


def test_frames_between_target_fps_are_grabbed_not_decoded_to_bgr(monkeypatch):
    cap = CountingCap()
    w = RtspCameraWorker("t_grab", None, "rtsp://example/x")
    monkeypatch.setattr(w, "_open_capture", lambda: cap)
    w.autostart = True
    w.ensure_capture(target_fps=5)
    try:
        deadline = time.time() + 5
        while time.time() < deadline and cap.reads < 3:
            time.sleep(0.02)
        assert cap.reads >= 3
        assert cap.grabs > cap.reads * 3, "между нужными кадрами остальные только вычитываются (grab)"
    finally:
        w.force_close()


def test_without_target_fps_every_frame_is_read(monkeypatch):
    cap = CountingCap()
    w = RtspCameraWorker("t_grab2", None, "rtsp://example/x")
    monkeypatch.setattr(w, "_open_capture", lambda: cap)
    w.autostart = True
    w.ensure_capture()
    try:
        time.sleep(0.5)
        assert cap.reads > 5 and cap.grabs == 0
    finally:
        w.force_close()


def test_substream_url_only_for_dahua_style_addresses():
    from camera_core.utils import substream_url
    u = "rtsp://admin:p@10.0.0.5:554/cam/realmonitor?channel=1&subtype=0"
    assert substream_url(u).endswith("channel=1&subtype=1")
    assert substream_url("rtsp://h/stream1") == "rtsp://h/stream1"
    assert substream_url("") == ""


def test_worker_opens_substream_only_when_enabled(monkeypatch):
    url = "rtsp://admin:p@10.0.0.5:554/cam/realmonitor?channel=1&subtype=0"
    opened = []

    def run(cfg):
        monkeypatch.setattr(rw.plate_config, "load", lambda: cfg)
        monkeypatch.setattr(rw.cv2, "VideoCapture", lambda u, api, params=None: opened.append(u) or Cap(True))
        w = RtspCameraWorker("t_sub", None, url)
        w._open_capture()
        return w
    w = run({"rtsp": {"hw_accel": "off"}})
    assert opened[-1] == url and not w.substream
    w = run({"rtsp": {"hw_accel": "off", "substream": True}})
    assert opened[-1].endswith("subtype=1") and w.substream and w.stream_state()["substream"] is True
    w = RtspCameraWorker("t_sub2", None, "rtsp://h/stream1")
    monkeypatch.setattr(rw.plate_config, "load", lambda: {"rtsp": {"substream": True}})
    w._open_capture()
    assert opened[-1] == "rtsp://h/stream1" and not w.substream, "не Dahua-адрес не трогаем"
