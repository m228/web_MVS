"""Честные FPS в RTSP-ячейке: metrics['fps'] — частота кадров ОТ КАМЕРЫ, metrics['fps_out'] — что уходит в браузер/запись."""
import random
import time

import numpy as np

from camera_core.rtsp_worker import RtspCameraWorker


class PacedCap:
    """Камера с заданной частотой и дрожанием интервала: grab/read ждут следующий кадр по расписанию."""

    def __init__(self, fps, jitter=0.0):
        self.period = 1.0 / fps
        self.jitter = jitter
        self.next_t = time.time()
        self.grabs = 0
        self.reads = 0
        self.rnd = random.Random(7)

    def isOpened(self):
        return True

    def _wait(self):
        self.next_t += self.period * (1.0 + self.rnd.uniform(-self.jitter, self.jitter))
        delay = self.next_t - time.time()
        if delay > 0:
            time.sleep(delay)

    def grab(self):
        self._wait()
        self.grabs += 1
        return True

    def retrieve(self):
        self.reads += 1
        return True, np.zeros((48, 64, 3), np.uint8)

    def read(self):
        self._wait()
        self.reads += 1
        return True, np.zeros((48, 64, 3), np.uint8)

    def get(self, prop):
        return 0

    def set(self, prop, value):
        return True

    def release(self):
        pass


def _run(monkeypatch, name, cam_fps, target_fps, jitter, seconds=3.2):
    cap = PacedCap(cam_fps, jitter)
    w = RtspCameraWorker(name, None, "rtsp://example/x")
    monkeypatch.setattr(w, "_open_capture", lambda: cap)
    w.autostart = True
    w.ensure_capture(target_fps=target_fps)
    try:
        time.sleep(seconds)
        return w.metrics["fps"], w.metrics["fps_out"]
    finally:
        w.force_close()


def test_fps_is_camera_rate_not_browser_rate(monkeypatch):
    fps, fps_out = _run(monkeypatch, "t_fps_in", cam_fps=20, target_fps=5, jitter=0.0)
    assert 16 <= fps <= 24, "fps — частота камеры (20), а не отдачи"
    assert fps_out <= 6.5, "в браузер уходит не больше целевых 5"


def test_target_equal_to_camera_rate_is_not_halved_by_jitter(monkeypatch):
    # камера 10 к/с с дрожанием ±12 %, просим 10: раньше кадр с интервалом 0,09 с пропускался и отдача падала вдвое
    fps, fps_out = _run(monkeypatch, "t_fps_jit", cam_fps=10, target_fps=10, jitter=0.12)
    assert fps >= 8.5
    assert fps_out >= 7.5, f"отдача {fps_out:.1f} при камере {fps:.1f}"


def test_fps_resets_when_capture_stops(monkeypatch):
    cap = PacedCap(20)
    w = RtspCameraWorker("t_fps_stop", None, "rtsp://example/x")
    monkeypatch.setattr(w, "_open_capture", lambda: cap)
    w.autostart = True
    w.ensure_capture()
    time.sleep(1.0)
    assert w.metrics["fps"] > 5
    w.force_close()
    time.sleep(0.5)
    assert w.metrics["fps"] == 0.0 and w.metrics["fps_out"] == 0.0
