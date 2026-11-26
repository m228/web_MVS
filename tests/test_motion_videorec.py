"""Запись видео со звуком на диск (motion_service/motion_videorec.py): настройки, команда ffmpeg, лимит места и настоящая запись из файла."""
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "motion_service"))

import motion_videorec as vr  # noqa: E402

FFMPEG = vr.find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="нет ffmpeg (задайте MOTION_FFMPEG)")


def wait_for(cond, timeout=30.0, step=0.2):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(step)
    return False


def make_source(path: Path, acodec: str, seconds: int = 30):
    audio = ["-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=%d:duration=%d" % (8000 if acodec != "aac" else 16000, seconds)] if acodec else []
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10:duration=%d" % seconds, *audio,
           "-c:v", "libx264", "-preset", "ultrafast", *(["-c:a", acodec] if acodec else []), str(path)]
    subprocess.run(cmd, check=True, timeout=60)


def describe(path: Path) -> str:
    return subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30).stderr


def test_sub_url():
    assert vr.sub_url("rtsp://u:p@10.0.0.1:554/cam/realmonitor?channel=1&subtype=0") == ("rtsp://u:p@10.0.0.1:554/cam/realmonitor?channel=1&subtype=1", True)
    assert vr.sub_url("rtsp://u:p@10.0.0.1/Streaming/Channels/101") == ("rtsp://u:p@10.0.0.1/Streaming/Channels/102", True)
    assert vr.sub_url("rtsp://u:p@10.0.0.1/Streaming/Channels/101?transportmode=unicast")[0].endswith("/Channels/102?transportmode=unicast")
    assert vr.sub_url("rtsp://10.0.0.1/Streaming/Channels/102") == ("rtsp://10.0.0.1/Streaming/Channels/102", True)
    assert vr.sub_url("rtsp://10.0.0.1/Streaming/Channels/201")[0].endswith("/Channels/202")
    assert vr.sub_url("rtsp://10.0.0.1/live/main") == ("rtsp://10.0.0.1/live/main", False)


def test_mask_hides_password():
    for pw in ("secret", "se@cret", "p/w:1", "pa ss", "qu'ote", 'qu"ote'):
        out = vr.mask("Input #0, rtsp, from 'rtsp://admin:%s@10.0.0.1:554/x':" % pw)
        assert pw not in out and "10.0.0.1:554/x" in out
    out = vr.mask("rtsp://10.0.0.1:554/user=admin&password=secret&channel=1")
    assert "secret" not in out and "channel=1" in out
    assert vr.mask("rtsp://10.0.0.1/x: refused") == "rtsp://10.0.0.1/x: refused"


@pytest.mark.parametrize("bad", [{"area": [0, 0, 0.01, 0.5]}, {"area": [0.8, 0, 0.5, 0.5]}, {"area": [0, 0, 1]}, {"area": "всё"},
                                 {"fps": 0}, {"fps": 100}, {"fps": "x"}, {"stream": "third"}])
def test_normalize_rejects(bad):
    with pytest.raises(ValueError):
        vr.normalize_camera(bad)


def test_cmd_copies_sound_and_crops(tmp_path):
    cfg = vr.normalize_camera({"on": True, "area": [0.25, 0.2, 0.5, 0.6]})
    cmd = vr.build_cmd("ffmpeg", "rtsp://u:p@h/x", cfg, tmp_path)
    assert cmd[cmd.index("-c:a") + 1] == "copy" and "0:a:0?" in cmd
    assert "crop=trunc(iw*0.5/2)*2:trunc(ih*0.6/2)*2:trunc(iw*0.25/2)*2:trunc(ih*0.2/2)*2" in cmd[cmd.index("-vf") + 1]
    assert cmd[cmd.index("-rtsp_transport") + 1] == "tcp" and "-re" not in cmd
    assert "-re" in vr.build_cmd("ffmpeg", str(tmp_path / "a.mkv"), cfg, tmp_path)


def test_settings_saved_and_partial_patch(tmp_path):
    cams = [{"id": "c1", "url": ""}]
    rec = vr.VideoRec(tmp_path, lambda: cams, ffmpeg=lambda: None)
    rec.set_camera("c1", {"area": [0.1, 0.1, 0.5, 0.5], "fps": 3})
    rec.set_camera("c1", {"stream": "main", "чужое": 1})
    rec.set_limit(7)
    again = vr.VideoRec(tmp_path, lambda: cams, ffmpeg=lambda: None)
    again.load()
    assert again.settings["c1"] == {**vr.CAM_DEFAULTS, "stream": "main", "area": [0.1, 0.1, 0.5, 0.5], "fps": 3} and again.limit_gb == 7
    with pytest.raises(ValueError):
        again.set_camera("c1", {"fps": 99})
    assert again.settings["c1"]["fps"] == 3
    with pytest.raises(ValueError):
        again.set_limit(0)


def _old_records(rec, count=3, size=1000):
    for i in range(count):
        f = rec.cam_dir("c1") / ("20260101_00%d000.mkv" % i)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * size)


@pytest.mark.parametrize("content", ["{не json", "", "[1]", '{"cameras": {}}', '{"limit_gb": null, "cameras": {}}', None])
def test_lost_limit_never_deletes(tmp_path, content):
    """Файл настроек испорчен, без лимита или пропал, а записи есть: лимит человека неизвестен — по умолчанию ничего не стираем."""
    if content is not None:
        (tmp_path / "video_rec.json").write_text(content, encoding="utf-8")
    cams = [{"id": "c1", "url": ""}]
    rec = vr.VideoRec(tmp_path, lambda: cams, ffmpeg=lambda: None)
    _old_records(rec)
    rec.load()
    rec.limit_gb = 500 / (1 << 30)                           # будь лимит «настоящим», удалилось бы всё лишнее
    assert not rec.limit_known and rec.purge() == 0 and len(rec.files()) == 3
    st = rec.set_camera("c1", {"fps": 3})                    # сохранение настроек камеры не должно «узаконить» лимит по умолчанию
    assert st["limit_known"] is False and "не удаляются" in st["cameras"]["c1"]["warn"]
    again = vr.VideoRec(tmp_path, lambda: cams, ffmpeg=lambda: None)
    again.load()
    assert not again.limit_known and again.settings["c1"]["fps"] == 3
    again.set_limit(5)
    third = vr.VideoRec(tmp_path, lambda: cams, ffmpeg=lambda: None)
    third.load()
    assert third.limit_known and third.limit_gb == 5


def test_first_start_has_default_limit(tmp_path):
    rec = vr.VideoRec(tmp_path, lambda: [], ffmpeg=lambda: None)
    rec.load()
    assert rec.limit_known and rec.settings == {} and rec.limit_gb == vr.DEFAULT_LIMIT_GB


def test_disabled_camera_does_not_record(tmp_path):
    cams = [{"id": "c1", "url": "rtsp://h/x", "enabled": False}]
    rec = vr.VideoRec(tmp_path, lambda: cams, ffmpeg=lambda: None)
    st = rec.set_camera("c1", {"on": True})
    assert not rec.recorders and "КЗ камеры выключено" in st["cameras"]["c1"]["error"]


def test_low_disk_resumes_with_margin(tmp_path, monkeypatch):
    rec = vr.VideoRec(tmp_path, lambda: [{"id": "c1", "url": "rtsp://h/x"}], ffmpeg=lambda: "ffmpeg")
    started = []
    monkeypatch.setattr(vr.CamRecorder, "start", lambda self: started.append(self.cid))
    monkeypatch.setattr(vr.CamRecorder, "join", lambda self, timeout=None: None)
    free = [vr.MIN_FREE_BYTES - 1]
    monkeypatch.setattr(rec, "_free", lambda: free[0])
    rec.set_camera("c1", {"on": True})
    free[0] = vr.MIN_FREE_BYTES + 1                          # чуть выше порога — ещё стоим
    rec.sync()
    assert not started
    free[0] = vr.MIN_FREE_BYTES + vr.RESUME_EXTRA_BYTES + 1
    rec.sync()
    assert started == ["c1"]


def test_main_stream_over_sub_address_warns(tmp_path):
    rec = vr.VideoRec(tmp_path, lambda: [{"id": "c1", "url": "rtsp://h/Streaming/Channels/102"}], ffmpeg=lambda: None)
    assert "второй поток" in rec.set_camera("c1", {"stream": "main"})["cameras"]["c1"]["warn"]


def test_no_ffmpeg_reports_error(tmp_path):
    rec = vr.VideoRec(tmp_path, lambda: [{"id": "c1", "url": "rtsp://h/x"}], ffmpeg=lambda: None)
    st = rec.set_camera("c1", {"on": True})
    assert "ffmpeg" in st["cameras"]["c1"]["error"] and not rec.recorders


def test_low_disk_blocks_recording_and_keeps_files(tmp_path, monkeypatch):
    rec = vr.VideoRec(tmp_path, lambda: [{"id": "c1", "url": "rtsp://h/x"}], ffmpeg=lambda: "ffmpeg")
    old = rec.cam_dir("c1") / "20260101_000000.mkv"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"x" * 100)
    monkeypatch.setattr(rec, "_free", lambda: vr.MIN_FREE_BYTES - 1)
    st = rec.set_camera("c1", {"on": True})
    rec.purge()
    assert "свободного места" in st["cameras"]["c1"]["error"] and not rec.recorders and old.exists()


def test_purge_removes_oldest_only_own_files(tmp_path):
    rec = vr.VideoRec(tmp_path, lambda: [])
    names = [("a", "20260101_000000.mkv"), ("b", "20260101_001000.mkv"), ("a", "20260101_002000.mkv")]
    for cam, name in names:
        f = rec.cam_dir(cam) / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * 1000)
    other = rec.cam_dir("a") / "мой ролик.mkv"
    other.write_bytes(b"x" * 5000)
    rec.limit_gb = 2500 / (1 << 30)
    assert rec.purge() == 1
    assert [n for n, _, _ in rec.files()] == ["20260101_001000.mkv", "20260101_002000.mkv"] and other.exists()


def test_cam_dir_stays_inside(tmp_path):
    rec = vr.VideoRec(tmp_path, lambda: [])
    for cid in ("..", "../..", "a\\b", "", ".hidden"):
        assert rec.cam_dir(cid).parent == rec.root and rec.cam_dir(cid).name not in ("", "..")


@needs_ffmpeg
@pytest.mark.parametrize("acodec,expect", [("aac", "aac"), ("pcm_alaw", "pcm_alaw")])
def test_real_recording_segments_sound_and_area(tmp_path, acodec, expect):
    src = tmp_path / "src.mkv"
    make_source(src, acodec)
    cams = [{"id": "rtsp_2", "url": str(src)}]
    rec = vr.VideoRec(tmp_path / "data", lambda: cams, ffmpeg=lambda: FFMPEG, segment_s=3)
    try:
        rec.set_camera("rtsp_2", {"on": True, "area": [0.25, 0.25, 0.5, 0.5]})
        assert wait_for(lambda: len(rec.files()) >= 3), rec.status()
        st = rec.status()["cameras"]["rtsp_2"]
        assert st["running"] and expect in st["audio"] and not st["error"], st
        t0 = time.monotonic()
        rec.set_camera("rtsp_2", {"on": False})
        assert not rec.recorders
        assert time.monotonic() - t0 < 6                         # ffmpeg вышел по «q» сам; убивать его (после 8 с) не пришлось
    finally:
        rec.shutdown()
    files = rec.files()
    first = describe(files[1][1] / files[1][0])
    assert "320x180" in first and "5 fps" in first and expect in first, first
    last = describe(files[-1][1] / files[-1][0])               # последний кусок закрыт по «q» — длительность записана
    assert "Duration: N/A" not in last and expect in last, last
    n = len(files)
    time.sleep(4)
    assert len(rec.files()) == n                                # после выключения ничего не пишется


@needs_ffmpeg
def test_real_recording_without_sound_warns(tmp_path):
    src = tmp_path / "src.mkv"
    make_source(src, "")
    rec = vr.VideoRec(tmp_path / "data", lambda: [{"id": "c1", "url": str(src)}], ffmpeg=lambda: FFMPEG, segment_s=3)
    try:
        rec.set_camera("c1", {"on": True})
        assert wait_for(lambda: rec.status()["cameras"]["c1"]["running"]), rec.status()
        st = rec.status()["cameras"]["c1"]
        assert st["audio"] == "" and "звук" in st["warn"], st
    finally:
        rec.shutdown()
    assert rec.files()


@needs_ffmpeg
def test_dead_source_retries_with_error(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "RETRY_MIN_S", 0.3)
    rec = vr.VideoRec(tmp_path, lambda: [{"id": "c1", "url": str(tmp_path / "нет.mkv")}], ffmpeg=lambda: FFMPEG)
    try:
        rec.set_camera("c1", {"on": True})
        assert wait_for(lambda: rec.status()["cameras"]["c1"]["error"], 15)
        assert rec.recorders["c1"].is_alive() and not rec.status()["cameras"]["c1"]["running"]
    finally:
        rec.shutdown()


def test_api(tmp_path):
    """Настоящий сервис на свободном порту и тонкий клиент основного приложения (motion_client)."""
    uvicorn = pytest.importorskip("uvicorn")
    sys.path.insert(0, str(ROOT))
    import motion_client as mc
    import server as srv
    (tmp_path / "motion_config.json").write_text(json.dumps({"cameras": [{"id": "rtsp_2", "name": "Печь", "url": "", "enabled": False}]}), encoding="utf-8")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(srv.create_app(tmp_path), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    url = "http://127.0.0.1:%d" % port
    try:
        assert wait_for(lambda: server.started, 10)
        st = mc.video_rec(url)
        assert st["cameras"]["rtsp_2"]["on"] is False and st["limit_gb"] == vr.DEFAULT_LIMIT_GB
        r = mc.video_rec_camera(url, "rtsp_2", {"area": [0, 0, 0.5, 0.5]})
        assert r["cameras"]["rtsp_2"]["area"] == [0, 0, 0.5, 0.5]
        assert "область" in mc.video_rec_camera(url, "rtsp_2", {"area": [0, 0, 2, 2]})["error"]
        assert mc.video_rec_camera(url, "нет", {"on": True})["error"] == "нет такой камеры"
        assert mc.video_rec_limit(url, 5)["limit_gb"] == 5
        assert "лимит" in mc.video_rec_limit(url, None)["error"]
    finally:
        server.should_exit = True
        th.join(30)
    assert not th.is_alive()
