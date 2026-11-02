"""Декодирование RTSP на видеоядре: режим из plate_config, откат на процессор, учёт реального декодера."""
import cv2

from camera_core import hw_decode
from camera_core import rtsp_worker as rw
from camera_core.rtsp_worker import RtspCameraWorker


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
