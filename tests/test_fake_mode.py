"""Фейк-режим (fake_mode.py): подмены не должны влиять на обычный запуск и должны отдавать рабочие камеры/плату."""
import json

import numpy as np
import pytest

import dahua_control
import fake_frames
import fake_mode


@pytest.fixture
def fake_on(monkeypatch):
    monkeypatch.setattr(fake_mode, "ENABLED", True)


def test_disabled_by_default():
    # в тестах WEB_MVS_FAKE не задан: обычное поведение не тронуто
    assert fake_mode.is_blocked("/api/update/run") is False
    assert fake_mode.inject_badge("<body></body>") == "<body></body>"


def test_blocked_and_badge_when_enabled(fake_on):
    for p in ("/api/update/run", "/api/update/apply", "/api/autostart/enable", "/api/net/enable_jumbo"):
        assert fake_mode.is_blocked(p), p
    assert not fake_mode.is_blocked("/api/update/check")
    assert not fake_mode.is_blocked("/api/cams")
    assert "FAKE" in fake_mode.inject_badge("<body></body>")


def test_seed_is_idempotent_and_points_to_local_emulators(tmp_path):
    fake_mode.seed(tmp_path)
    cfg = json.loads((tmp_path / "plate_config.json").read_text(encoding="utf-8"))
    assert cfg["host"] == "127.0.0.1" and cfg["sv_source"]["host"] == "127.0.0.1"
    assert cfg["camera_serial"] in fake_mode.GIGE_DEVICES
    assert not any(s["autostart"] for s in cfg["services"].values())     # сайдкары поднимает fake_server, не app
    rtsp = json.loads((tmp_path / "rtsp_cameras.json").read_text(encoding="utf-8"))
    assert rtsp and all(fake_mode.is_fake_url(e["url"]) for e in rtsp)    # ни одного настоящего адреса
    (tmp_path / "plate_config.json").write_text('{"host": "x"}', encoding="utf-8")
    fake_mode.seed(tmp_path)                                              # повторный seed ничего не затирает
    assert json.loads((tmp_path / "plate_config.json").read_text(encoding="utf-8")) == {"host": "x"}


def test_is_fake_url():
    assert fake_mode.is_fake_url("rtsp://admin:p@127.0.0.2:554/x")
    assert not fake_mode.is_fake_url("rtsp://admin:p@192.168.1.108:554/x")
    assert not fake_mode.is_fake_url("")


def test_frames_roi_exposure_and_label():
    full = fake_frames.render("T-1", 1, "micro")
    assert full.shape == (fake_frames.SENSOR_H, fake_frames.SENSOR_W, 3)
    roi = fake_frames.render("T-1", 1, "micro", 640, 480, 100, 50)
    assert roi.shape == (480, 640, 3)
    dark = fake_frames.render("T-1", 1, "micro", 640, 480, 100, 50, exposure_time=2000)
    assert dark.mean() < roi.mean()                                       # выдержка меняет яркость
    assert not np.array_equal(fake_frames.render("T-1", 1, "rtsp"), fake_frames.render("T-1", 2, "rtsp"))   # кадры меняются


def test_fake_capture_api():
    cap = fake_frames.FakeCapture("c", fps=50)
    ok, img = cap.read()
    assert cap.isOpened() and ok and img.shape == (fake_frames.RTSP_H, fake_frames.RTSP_W, 3)
    assert cap.grab() and cap.retrieve()[0]
    cap.release()
    assert not cap.isOpened() and cap.read() == (False, None)


def test_fake_dahua_roundtrip(fake_on, monkeypatch):
    monkeypatch.setattr(dahua_control, "_cgi", fake_mode.fake_cgi)
    host = "127.0.0.9"
    caps = dahua_control.get_capabilities(host, "u", "p")
    assert caps["reachable"] and caps["white_light"] and caps["optical_zoom"] and caps["focus"] and caps["auto_focus"]
    assert dahua_control.get_white_light(host, "u", "p") == "off"
    assert dahua_control.set_white_light(host, "u", "p", True, 40)
    assert dahua_control.get_white_light(host, "u", "p") == "on"
    z0 = dahua_control.get_lens_status(host, "u", "p")["zoom"]
    assert dahua_control.optical_zoom(host, "u", "p", "tele", 4)
    assert dahua_control.get_lens_status(host, "u", "p")["zoom"] > z0
    assert dahua_control.set_exposure(host, "u", "p", compensation=70)["ok"]
    assert dahua_control.get_image_settings(host, "u", "p")["exposure"]["compensation"] == 70
    assert dahua_control.set_network(host, "u", "p", ip="10.0.0.5", mask="255.255.255.0", dhcp=False)["ok"]
    assert dahua_control.get_network(host, "u", "p")["ip"] == "10.0.0.5"


def test_fake_gige_worker_streams_and_applies_settings(data_dir):
    from camera_core.fake_worker import FakeGigeWorker

    class _Mgr:
        cam_online = {"FAKE-GIGE-1": 1}

    w = FakeGigeWorker("FAKE-GIGE-1", _Mgr())
    assert w.get_ip() == {"ip": "192.168.1.222"}
    assert w.get_network_settings()[0] == "192.168.1.222"
    assert any(i["label"] == "Модель" for i in w.get_info()["items"])
    gen = w.generate(width=640, height=480, offset_x=16, offset_y=8, fps=15, exposure_auto="Off", exposure_time=5000)
    chunk = next(gen)
    assert chunk.startswith(b"--frame") and b"image/jpeg" in chunk
    assert w.running and w.current_config["width"] == 640 and w.current_config["offset_x"] == 16
    assert (w.metrics["width"], w.metrics["height"]) == (640, 480)
    w.force_close()
    gen.close()
    assert not w.running
    assert w.change_ip("192.168.1.230") == {"ip": "ip_changed"} and w.get_ip() == {"ip": "192.168.1.230"}
