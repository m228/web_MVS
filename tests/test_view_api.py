"""Эндпоинты показа в приложении: поток отдаётся закрывающимся ответом, «Только анализ» сохраняется, запись поднимает захват."""
import pytest

from camera_core.mjpeg import MjpegResponse

URL = "rtsp://example/cam?channel=1&subtype=0"


@pytest.fixture
def web(data_dir, monkeypatch):
    import app as web
    from camera_core.rtsp_worker import RtspCameraWorker
    started = []
    monkeypatch.setattr(RtspCameraWorker, "ensure_capture", lambda self, *a, **k: started.append(self.serial_number) or False)
    web.started = started
    yield web
    web.manager.analysis_only = False
    web.manager.drop_rtsp("T-API")


def test_stream_uses_closing_response(web):
    response = web.rtsp_stream("T-API", url=URL, ip=None, username="admin", password="", channel=1, subtype=0, scale=100, fps=None)
    assert isinstance(response, MjpegResponse), "без MjpegResponse ушедший зритель висит до сборки мусора и держит камеру"
    assert response.media_type.startswith("multipart/x-mixed-replace")


def test_analysis_only_is_saved_and_applied(web):
    import plate_config
    assert web.rtsp_analysis_only_set({"analysis_only": True}) == {"status": "ok", "analysis_only": True}
    assert web.manager.analysis_only is True and plate_config.load()["rtsp"]["analysis_only"] is True
    assert plate_config.load()["rtsp"]["hw_accel"] == "auto", "остальные настройки rtsp не затёрты"
    web.rtsp_analysis_only_set({"analysis_only": False})
    assert web.rtsp_analysis_only() == {"analysis_only": False}


def test_attach_registers_hidden_camera_and_saving_starts_capture(web, monkeypatch):
    web.rtsp_attach("T-API", url=URL, ip=None, username="admin", password="", channel=1, subtype=0, scale=50, fps=2)
    worker = web.manager.get_rtsp("T-API")
    assert worker is not None and worker.viewer_count() == 0
    monkeypatch.setattr(worker, "on_photo", lambda *a: {"status": "ok"})
    monkeypatch.setattr(worker, "on_video", lambda *a: {"status": "ok"})
    web.started.clear()
    web.rtsp_on_save_photo("T-API", 60)
    web.rtsp_on_save_video("T-API", 5)
    assert web.started == ["T-API", "T-API"]


def test_load_endpoint_reports_mode(web, monkeypatch):
    monkeypatch.setattr(web._load_meter, "read", lambda: {"ready": False})
    web.manager.analysis_only = True
    assert web.host_load_state() == {"ready": False, "analysis_only": True}
