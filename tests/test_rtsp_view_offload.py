"""«Глазик» и «Только анализ»: ушёл зритель — приложение отпускает камеру (настоящий uvicorn, вместо камеры заглушка)."""
import socket
import threading
import time

import numpy as np
import pytest
import uvicorn
from fastapi import FastAPI

from camera_core.mjpeg import MjpegResponse
from camera_core.rtsp_worker import RtspCameraWorker


class FakeCapture:
    def __init__(self):
        self.alive = True

    def isOpened(self):
        return self.alive

    def set(self, *args):
        return True

    def get(self, *args):
        return 25.0

    def grab(self):
        time.sleep(0.02)
        return True

    def retrieve(self):
        return True, np.zeros((48, 64, 3), np.uint8)

    def read(self):
        self.grab()
        return self.retrieve()

    def release(self):
        self.alive = False


class FakeManager:
    analysis_only = False


def _wait(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _alive(worker):
    return worker.stream_state()["capture_active"]


@pytest.fixture
def worker(monkeypatch):
    w = RtspCameraWorker("t_offload", FakeManager(), "rtsp://example/stream")
    monkeypatch.setattr(w, "_open_capture", lambda: FakeCapture())
    yield w
    w.force_close()


@pytest.fixture
def watch(worker):
    """Поднимает настоящий сервер; watch() открывает поток и возвращает сокет зрителя."""
    app = FastAPI()
    app.get("/s")(lambda: MjpegResponse(worker.generate()))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", ws="none"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    assert _wait(lambda: server.started)

    def open_viewer():
        s = socket.create_connection(sock.getsockname(), timeout=5)
        s.sendall(b"GET /s HTTP/1.1\r\nHost: x\r\n\r\n")
        data = b""
        while b"--frame" not in data:
            data += s.recv(65536)
        return s

    yield open_viewer
    server.should_exit = True
    thread.join(5)


def test_viewer_gone_releases_camera_without_autostart(worker, watch):
    s = watch()
    assert worker.viewer_count() == 1
    s.close()
    assert _wait(lambda: worker.viewer_count() == 0), "зритель ушёл, а счётчик не убавился"
    assert _wait(lambda: not _alive(worker)), "зрителей нет и держать камеру некому — захват должен остановиться"


def test_viewers_do_not_pile_up_on_reopen(worker, watch):
    for _ in range(4):
        s = watch()
        s.close()
        assert _wait(lambda: worker.viewer_count() == 0)


def test_quick_reopen_gets_frames(worker, watch):
    watch().close()
    s = watch()                      # «глазик» выкл/вкл подряд: старый поток захвата мог ещё закрываться
    assert _alive(worker)
    s.close()


def test_autostart_holds_camera_only_outside_analysis_mode(worker):
    worker.autostart = True
    worker.ensure_capture()
    assert _wait(lambda: worker.metrics["image_number"] > 0)
    worker.manager.analysis_only = True
    assert _wait(lambda: not _alive(worker)), "«Только анализ»: автостарт камеру не держит"
    assert worker.last_frame is None, "после остановки снимок не должен брать старый кадр"
    assert worker.metrics["fps"] == 0.0 and worker.metrics["bandwidth_mbps"] == 0.0
    assert worker.ensure_capture() is False and not _alive(worker)
    worker.manager.analysis_only = False
    assert worker.ensure_capture() is True


def test_viewer_in_analysis_mode_sees_picture_then_releases(worker, watch):
    worker.autostart = True
    worker.manager.analysis_only = True
    s = watch()
    s.close()
    assert _wait(lambda: not _alive(worker))


def test_photo_keeps_camera_in_analysis_mode(worker, monkeypatch):
    monkeypatch.setattr(worker, "write_photo", lambda *a, **k: None)
    worker.manager.analysis_only = True
    worker.save_photo, worker.photo_interval = True, 60
    assert worker.ensure_capture() is True
    assert _wait(lambda: worker.metrics["image_number"] > 0)
    worker.save_photo = False
    assert _wait(lambda: not _alive(worker))


def test_link_lost_state_cleared_when_capture_stops(monkeypatch):
    class Dead(FakeCapture):
        def isOpened(self):
            return False

    w = RtspCameraWorker("t_dead", FakeManager(), "rtsp://example/stream")
    monkeypatch.setattr(w, "_open_capture", lambda: Dead())
    w.autostart = True
    w.ensure_capture()
    assert _wait(lambda: w.reconnect_attempts > 0)
    w.manager.analysis_only = True
    assert _wait(lambda: not _alive(w))
    assert w.reconnect_attempts == 0 and not w.reconnecting


def test_dead_camera_stays_lost_when_viewer_just_leaves(monkeypatch):
    class Dead(FakeCapture):
        def isOpened(self):
            return False

    w = RtspCameraWorker("t_dead_view", FakeManager(), "rtsp://example/stream")
    monkeypatch.setattr(w, "_open_capture", lambda: Dead())
    w._viewers = 1
    w.ensure_capture()
    assert _wait(lambda: w.reconnect_attempts > 0)
    w._viewers = 0                       # повтор страницы: зритель ушёл, камера всё так же мертва
    assert _wait(lambda: not _alive(w))
    assert w.reconnect_attempts > 0, "обрыв для журнала не должен закрываться уходом зрителя"
    w.force_close()


def test_generator_ends_when_capture_thread_dies(worker, monkeypatch):
    frames = iter([(True, np.zeros((48, 64, 3), np.uint8))] * 5)

    class Breaks(FakeCapture):
        def read(self):
            time.sleep(0.02)
            item = next(frames, None)
            if item is None:
                raise RuntimeError("декодер упал")
            return item

        def grab(self):
            return True

    monkeypatch.setattr(worker, "_open_capture", lambda: Breaks())
    gen = worker.generate()
    started = time.time()
    assert list(gen) and time.time() - started < 10, "без кадров и без потока захвата генератор обязан закончиться"
    assert worker.viewer_count() == 0


def test_video_started_before_first_frame_is_not_stuck(worker, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "video_dir", lambda: tmp_path)
    worker.manager.analysis_only = True
    worker.on_video(1)
    worker.video_start -= 60             # камера подключалась дольше заданной длительности
    worker._check_video_finished()
    assert worker.save_video == 1, "пока файл не начат, длительность не считаем"
    worker.off_video()
    assert worker.save_video == 0 and not worker._saving()
    worker.on_video(1)
    worker.ensure_capture()
    assert _wait(lambda: worker.save_video == 0, timeout=8.0), "запись должна начаться, отсчитать длительность и закрыться"
    assert list(tmp_path.glob("*.avi"))
    assert _wait(lambda: not _alive(worker))


def test_low_fps_limit_is_still_measured(worker):
    worker.autostart = True
    worker.ensure_capture(target_fps=0.5)
    assert _wait(lambda: 0.3 <= worker.metrics["fps_out"] <= 0.7, timeout=9.0), worker.metrics["fps_out"]
