"""Scan Camera: разбор подсети, прогон задачи на заглушках (без сети/OpenCV), добавление камеры по двойному клику."""
import time

import scan_service as ss


def test_parse_prefix():
    assert ss.parse_prefix("10.32.2") == "10.32.2"
    assert ss.parse_prefix(" 10.32.2.0 ") == "10.32.2"
    assert ss.parse_prefix("10.32") is None
    assert ss.parse_prefix("10.32.2.x") is None
    assert ss.parse_prefix("300.1.1") is None


def _wait(job):
    for _ in range(100):
        if not job.running:
            return
        time.sleep(0.05)
    raise AssertionError("скан не завершился")


def _job(monkeypatch):
    monkeypatch.setattr(ss, "find_port", lambda ip, t: 554 if ip.endswith((".5", ".7")) else None)
    # .5 отдаёт кадр с паролем «b», .7 — кадра нет
    monkeypatch.setattr(ss, "probe", lambda ip, port, user, pws:
                        (b"JPEG", (1280, 720), "/p", "b") if ip.endswith(".5") else (None, None, None, None))
    return ss.ScanJob()


def test_job_finds_cameras(monkeypatch):
    job = _job(monkeypatch)
    assert job.start("10.0.0", 1, 10, "admin", ["a", "b"])
    _wait(job)
    st = job.state()
    assert [(i["ip"], i["status"]) for i in st["items"]] == [("10.0.0.5", "frame"), ("10.0.0.7", "noframe")]
    assert job.frame("10.0.0.5") == b"JPEG" and job.frame("10.0.0.7") is None
    assert job.connection("10.0.0.5") == {"port": 554, "path": "/p", "password": "b"}
    assert job.connection("10.0.0.7")["password"] is None


def test_scan_add(data_dir, monkeypatch):
    import app as web
    job = _job(monkeypatch)
    monkeypatch.setattr(ss, "job", job)
    job.start("10.0.0", 1, 10, "admin", ["b"])
    _wait(job)
    # пароль не введён -> берём найденный
    res = web.scan_add({"ip": "10.0.0.5", "user": "admin", "password": ""})
    assert res["items"][-1]["url"] == "rtsp://admin:b@10.0.0.5:554/p"
    # кадра не было: пароль вводит пользователь, спецсимволы кодируются
    res = web.scan_add({"ip": "10.0.0.7", "user": "admin", "password": "p@ss"})
    assert res["items"][-1]["url"].startswith("rtsp://admin:p%40ss@10.0.0.7:554/")
    assert web.scan_add({"ip": "1.2.3.4"}) == {"error": "not_found"}
    assert web.scan_start({"prefix": "bad"}) == {"error": "bad_range"}
