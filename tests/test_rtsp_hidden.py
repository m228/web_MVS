"""«Глазик» в мультипросмотре: пока RTSP-камеру никто не смотрит, JPEG не кодируется и не публикуется,
а сам захват (его держит автостарт/автосохранение/КЗ) продолжает идти."""
import time

import numpy as np

from camera_core.rtsp_worker import RtspCameraWorker


class FakeCapture:
    def __init__(self):
        self.n = 0

    def isOpened(self):
        return True

    def set(self, *args):
        return True

    def get(self, *args):
        return 25.0

    def read(self):
        time.sleep(0.01)
        self.n += 1
        return True, np.full((48, 64, 3), self.n % 255, np.uint8)

    def release(self):
        pass


def _wait(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_no_jpeg_without_viewers_and_capture_keeps_running(monkeypatch):
    worker = RtspCameraWorker("t_hidden", None, "rtsp://example/stream")
    monkeypatch.setattr(worker, "_open_capture", lambda: FakeCapture())
    worker.autostart = True  # держит захват без зрителей
    worker.ensure_capture()
    try:
        # зрителей нет: кадры считаются, но JPEG не кодируется и в браузер ничего не идёт
        assert _wait(lambda: worker.metrics["image_number"] >= 3)
        assert worker._latest_jpeg is None
        assert worker.metrics["bandwidth_mbps"] == 0.0

        # зритель вернулся (глазик нажат снова): кадры начинают публиковаться
        worker._viewers = 1
        assert _wait(lambda: worker._latest_jpeg is not None)
        assert _wait(lambda: worker.metrics["bandwidth_mbps"] > 0)

        # зритель ушёл: устаревший кадр сбрасывается, чтобы не показать картинку из прошлого
        worker._viewers = 0
        assert _wait(lambda: worker._latest_jpeg is None)
        assert worker.metrics["bandwidth_mbps"] == 0.0

        # захват при этом жив и кадры идут
        before = worker.metrics["image_number"]
        assert _wait(lambda: worker.metrics["image_number"] > before)
    finally:
        worker.force_close()
