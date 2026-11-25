"""Приближение RTSP-камеры переживает перезапуск приложения (новый воркер читает его из save_settings)."""
import json

import pytest

import save_settings
from camera_core.rtsp_worker import RtspCameraWorker

URL = "rtsp://127.0.0.1:554/live"


def _worker(serial="rtsp_1"):
    return RtspCameraWorker(serial, None, URL)


def _zoom(w):
    return w.zoom_factor, w.zoom_pan_x, w.zoom_pan_y


def test_zoom_region_survives_restart(data_dir):
    _worker().set_zoom(2.5, 0.2, 0.8)
    assert _zoom(_worker()) == (2.5, 0.2, 0.8)
    assert _zoom(_worker("rtsp_2")) == (1.0, 0.5, 0.5), "соседней камере чужой зум не достаётся"


def test_zoom_pan_and_reset_are_saved(data_dir):
    w = _worker()
    w.set_zoom(3)
    w.set_zoom(None, 0.9, 0.1)
    assert _zoom(_worker()) == (3.0, 0.9, 0.1)
    w.set_zoom(1)
    assert _zoom(_worker()) == (1.0, 0.5, 0.5)


def test_zoom_keeps_photo_settings(data_dir):
    save_settings.update("rtsp_1", photo_project="p1", photo_interval=5)
    _worker().set_zoom(2)
    got = save_settings.get("rtsp_1")
    assert got["photo_project"] == "p1" and got["photo_interval"] == 5


@pytest.mark.parametrize("bad", ["x", 5, {"factor": "abc", "pan_x": 0, "pan_y": 0}, {"factor": 2}])
def test_broken_record_gives_no_zoom(data_dir, bad):
    save_settings.STORE.write_text(json.dumps({"rtsp_1": {"zoom": bad}}), encoding="utf-8")
    assert _zoom(_worker()) == (1.0, 0.5, 0.5)


def test_removed_camera_forgets_zoom(data_dir):
    import app
    import rtsp_store

    rtsp_store.save({"url": URL, "serial": "rtsp_1"})
    _worker().set_zoom(2, 0.1, 0.1)
    app.rtsp_remove_saved(URL)
    assert _zoom(_worker()) == (1.0, 0.5, 0.5)
